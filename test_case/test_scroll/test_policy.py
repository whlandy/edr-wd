"""
PR2 acceptance — ScrollPolicy bounds + scroll_with_policy single-attempt
executor (docs/todo/scroll-and-paged-table-actions.md, enforceability item 2).
"""

from __future__ import annotations

import pytest


from action_dispatcher.receipts import ActionReceipt
from scroll.policy import ScrollPolicy, scroll_with_policy
from scroll.results import Reason, Strategy


def _ok(*, strategy=None, amount=None, strategy_ref=None):
    return ActionReceipt.from_ok(action_id="pointer.scroll", action_code="A029",
                                 request_id=None, result=None)


def _err(*, strategy=None, amount=None, strategy_ref=None):
    return ActionReceipt.from_error(code="backend_error", action_id="pointer.scroll",
                                    action_code="A029", request_id=None,
                                    message="no backend")


def _verify(val):
    return lambda: val


# ---------------------------------------------------------------------------
# ScrollPolicy construction
# ---------------------------------------------------------------------------


def test_policy_default_max_attempts_three():
    p = ScrollPolicy()
    assert p.max_attempts == 3
    assert len(p.strategy_order) == 4


def test_policy_default_order_is_escalation():
    p = ScrollPolicy()
    assert p.strategy_order == (
        Strategy.WHEEL,
        Strategy.SCROLLBAR_DRAG,
        Strategy.FOCUS_THEN_SCROLL,
        Strategy.PAGINATION,
    )


def test_policy_total_dispatch_bound():
    p = ScrollPolicy()
    assert p.total_dispatch_bound == len(p.strategy_order) * p.max_attempts


def test_policy_rejects_negative_max_attempts():
    with pytest.raises(ValueError):
        ScrollPolicy(max_attempts=-1)


def test_policy_rejects_empty_order():
    with pytest.raises(ValueError):
        ScrollPolicy(strategy_order=())


def test_policy_max_attempts_zero_is_valid():
    p = ScrollPolicy(max_attempts=0)
    assert p.max_attempts == 0
    assert p.total_dispatch_bound == 0


def test_policy_backoff_linear_and_exponential():
    linear = ScrollPolicy(backoff_initial_ms=100, backoff_factor=None)
    assert linear.backoff_ms(0) == 0
    assert linear.backoff_ms(1) == 100
    assert linear.backoff_ms(3) == 300

    exp = ScrollPolicy(backoff_initial_ms=100, backoff_factor=2.0)
    assert exp.backoff_ms(1) == 100
    assert exp.backoff_ms(2) == 200
    assert exp.backoff_ms(4) == 800


# ---------------------------------------------------------------------------
# scroll_with_policy — single-attempt executor
# ---------------------------------------------------------------------------


def test_never_armed_returns_not_dispatched():
    r = scroll_with_policy(policy=ScrollPolicy(), verify=_verify(True),
                           dispatch=_err, strategy=Strategy.WHEEL)
    assert r.reason is Reason.NOT_DISPATCHED
    assert not r.dispatched and not r.moved
    assert not r.success


def test_armed_and_moved_returns_success():
    r = scroll_with_policy(policy=ScrollPolicy(), verify=_verify(True),
                           dispatch=_ok, strategy=Strategy.WHEEL)
    assert r.success
    assert r.moved and r.dispatched
    assert r.strategy_used is Strategy.WHEEL
    assert r.reason is Reason.WHEEL_MOVED


def test_armed_not_moved_returns_no_scroll_effect():
    r = scroll_with_policy(policy=ScrollPolicy(), verify=_verify(False),
                           dispatch=_ok, strategy=Strategy.WHEEL)
    assert not r.success
    assert r.dispatched and not r.moved
    assert r.reason is Reason.NO_SCROLL_EFFECT


def test_exactly_one_dispatch_single_attempt():
    calls = []

    def dispatch(*, strategy, amount=None, strategy_ref=None):
        calls.append(strategy)
        return _ok()

    scroll_with_policy(policy=ScrollPolicy(), verify=_verify(True),
                       dispatch=dispatch, strategy=Strategy.WHEEL)
    assert calls == [Strategy.WHEEL]


def test_max_attempts_zero_yields_not_dispatched_without_dispatch():
    dispatched = []

    def dispatch(*, strategy, amount=None, strategy_ref=None):
        dispatched.append(1)
        return _ok()

    r = scroll_with_policy(policy=ScrollPolicy(max_attempts=0),
                           verify=_verify(True), dispatch=dispatch,
                           strategy=Strategy.WHEEL)
    assert r.reason is Reason.NOT_DISPATCHED
    assert dispatched == []  # nothing armed


def test_ok_false_never_contributes_to_moved():
    r = scroll_with_policy(policy=ScrollPolicy(), verify=_verify(True),
                           dispatch=_err, strategy=Strategy.WHEEL)
    assert r.moved is False  # ok=False never yields moved=True


def test_attribution_strategy_specific():
    r = scroll_with_policy(policy=ScrollPolicy(), verify=_verify(True),
                           dispatch=_ok, strategy=Strategy.PAGINATION)
    assert r.strategy_used is Strategy.PAGINATION
    assert r.reason is Reason.NEXT_PAGE
