"""
PR1 acceptance — scroll_and_verify composite with a mock dispatcher/observer
(docs/todo/scroll-and-paged-table-actions.md, enforceability item 1/6).
"""

from __future__ import annotations

import copy

import pytest


from action_dispatcher.receipts import ActionReceipt
from scroll.observer import Observer
from scroll.results import Reason, Strategy
from scroll.scroll_and_verify import scroll_and_verify


_BASE = [
    {"process_name": "X", "pid": 1, "native_window_id": "w",
     "title": "Main", "kind": "window"},
    {"process_name": "X", "pid": 1, "native_window_id": "w",
     "title": "OK", "kind": "control", "control_type": "Button",
     "text": "value"},
]


def _ok_receipt():
    return ActionReceipt.from_ok(action_id="pointer.scroll", action_code="A029",
                                 request_id=None, result=None)


def _err_receipt():
    return ActionReceipt.from_error(code="backend_error", action_id="pointer.scroll",
                                    action_code="A029", request_id=None,
                                    message="boom")


def _dispatch_ok(*, strategy, amount=None, strategy_ref=None):
    return _ok_receipt()


def _dispatch_err(*, strategy, amount=None, strategy_ref=None):
    return _err_receipt()


def _observer_with_targets_delta(change_index=None):
    """A stub Observer whose snapshot() alternates between two snapshots —
    the second differs from the first at `change_index` (None ⇒ identical).
    This lets content_changed report change on the second snapshot() call.

    Uses `copy.deepcopy` on the base targets because the target dicts are
    nested (a shallow `list()` copy would alias the inner dicts and leak a
    "moved" mutation into later test cases).
    """
    state = {"n": 0}

    def targets_fn():
        state["n"] += 1
        if state["n"] >= 2 and change_index is not None:
            mod = copy.deepcopy(_BASE)
            mod[change_index]["text"] = "moved"
            return mod
        return copy.deepcopy(_BASE)

    return Observer(targets_fn=targets_fn, backend="x", host="h")


def _observer_change_on_second_call():
    """Observer whose 2nd snapshot() differs from the 1st → moved."""
    return _observer_with_targets_delta(change_index=1)


def _observer_no_change():
    """Observer whose snapshots never differ → not moved."""
    return _observer_with_targets_delta(change_index=None)


# ---------------------------------------------------------------------------
# ok=False → NOT_DISPATCHED
# ---------------------------------------------------------------------------


def test_receipt_ok_false_returns_not_dispatched():
    def dispatch(*, strategy, amount=None, strategy_ref=None):
        return _err_receipt()

    obs = _observer_no_change()
    r = scroll_and_verify(dispatch=dispatch, observer=obs, strategy=Strategy.WHEEL)
    assert r.dispatched is False
    assert r.moved is False
    assert r.reason is Reason.NOT_DISPATCHED
    assert r.success is False


# ---------------------------------------------------------------------------
# ok=True but no content change → NO_SCROLL_EFFECT
# ---------------------------------------------------------------------------


def test_ok_true_no_change_returns_no_scroll_effect():
    def dispatch(*, strategy, amount=None, strategy_ref=None):
        return _ok_receipt()

    obs = _observer_no_change()
    r = scroll_and_verify(dispatch=dispatch, observer=obs, strategy=Strategy.WHEEL)
    assert r.dispatched is True
    assert r.moved is False
    assert r.reason is Reason.NO_SCROLL_EFFECT
    assert r.success is False


# ---------------------------------------------------------------------------
# ok=True + content change → success
# ---------------------------------------------------------------------------


def test_ok_true_with_change_returns_success():
    def dispatch(*, strategy, amount=None, strategy_ref=None):
        return _ok_receipt()

    obs = _observer_change_on_second_call()
    r = scroll_and_verify(dispatch=dispatch, observer=obs, strategy=Strategy.WHEEL)
    assert r.dispatched is True
    assert r.moved is True
    assert r.success is True
    assert r.strategy_used is Strategy.WHEEL
    assert r.reason is Reason.WHEEL_MOVED


def test_success_uses_strategy_specific_reason():
    def dispatch(*, strategy, amount=None, strategy_ref=None):
        return _ok_receipt()

    obs = _observer_change_on_second_call()
    r = scroll_and_verify(dispatch=dispatch, observer=obs,
                          strategy=Strategy.PAGINATION)
    assert r.success
    assert r.reason is Reason.NEXT_PAGE
    assert r.strategy_used is Strategy.PAGINATION


# ---------------------------------------------------------------------------
# verify=False → forced moved=False, NO_SCROLL_EFFECT
# ---------------------------------------------------------------------------


def test_verify_false_forces_moved_false():
    dispatched = []

    def dispatch(*, strategy, amount=None, strategy_ref=None):
        dispatched.append(strategy)
        return _ok_receipt()

    obs = _observer_change_on_second_call()
    r = scroll_and_verify(dispatch=dispatch, observer=obs, strategy=Strategy.WHEEL,
                          verify=False)
    assert r.dispatched is True
    assert r.moved is False
    assert r.reason is Reason.NO_SCROLL_EFFECT
    assert r.success is False
    # verify=False never fabricates movement.
    assert len(dispatched) == 1


def test_verify_false_ok_false_still_not_dispatched():
    def dispatch(*, strategy, amount=None, strategy_ref=None):
        return _err_receipt()

    obs = _observer_no_change()
    r = scroll_and_verify(dispatch=dispatch, observer=obs, strategy=Strategy.WHEEL,
                          verify=False)
    assert r.reason is Reason.NOT_DISPATCHED


# ---------------------------------------------------------------------------
# Single-strategy: exactly one dispatch, no invented multi-primitive fallback
# ---------------------------------------------------------------------------


def test_exactly_one_dispatch():
    seen = []

    def dispatch(*, strategy, amount=None, strategy_ref=None):
        seen.append(strategy)
        return _ok_receipt()

    obs = _observer_change_on_second_call()
    scroll_and_verify(dispatch=dispatch, observer=obs, strategy=Strategy.WHEEL)
    assert seen == [Strategy.WHEEL]


def test_amount_forwarded_to_dispatch():
    captured = {}

    def dispatch(*, strategy, amount=None, strategy_ref=None):
        captured["amount"] = amount
        captured["strategy"] = strategy
        return _ok_receipt()

    obs = _observer_change_on_second_call()
    scroll_and_verify(dispatch=dispatch, observer=obs, strategy=Strategy.WHEEL,
                      amount=-5)
    assert captured == {"amount": -5, "strategy": Strategy.WHEEL}
