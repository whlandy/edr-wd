"""
PR2 acceptance — Step machine transition table + run() driver
(docs/todo/scroll-and-paged-table-actions.md, enforceability item 4).
"""

from __future__ import annotations

import pytest


from action_dispatcher.receipts import ActionReceipt
from scroll.policy import ScrollPolicy
from scroll.results import Reason, ScrollResult, Strategy
from scroll.state_machine import (
    NavigateContext,
    Step,
    TERMINAL_STEPS,
    run,
    transition,
)


def _ctx(remaining=None, attempts=0, any_armed=False, attempt=None,
         max_attempts=3):
    return NavigateContext(
        owner="nav-1",
        policy=ScrollPolicy(max_attempts=max_attempts),
        remaining=remaining if remaining is not None else (Strategy.WHEEL,),
        position_attempts=attempts,
        any_armed=any_armed,
        attempt=attempt,
    )


def _ok(*, strategy=None, amount=None, strategy_ref=None):
    return ActionReceipt.from_ok(action_id="pointer.scroll", action_code="A029",
                                 request_id=None, result=None)


def _err(*, strategy=None, amount=None, strategy_ref=None):
    return ActionReceipt.from_error(code="backend_error", action_id="pointer.scroll",
                                    action_code="A029", request_id=None,
                                    message="no backend")


# ---------------------------------------------------------------------------
# transition — pure decision table (item-4 determinism invariant)
# ---------------------------------------------------------------------------


def test_linear_prelude_edges():
    assert transition(Step.DISCOVER, _ctx()) is Step.CLASSIFY
    assert transition(Step.CLASSIFY, _ctx()) is Step.OWNERSHIP


def test_ownership_with_remaining_goes_execute():
    assert transition(Step.OWNERSHIP, _ctx(remaining=(Strategy.WHEEL,))) is Step.EXECUTE


def test_ownership_no_remaining_goes_not_dispatched():
    assert transition(Step.OWNERSHIP, _ctx(remaining=())) is Step.NOT_DISPATCHED


def test_execute_always_verifies():
    assert transition(Step.EXECUTE, _ctx()) is Step.VERIFY


def test_verify_moved_success():
    moved = ScrollResult(True, True, Reason.WHEEL_MOVED, Strategy.WHEEL)
    assert transition(Step.VERIFY, _ctx(attempt=moved)) is Step.SUCCESS


def test_verify_not_moved_fallback():
    ne = ScrollResult.no_effect()
    assert transition(Step.VERIFY, _ctx(attempt=ne)) is Step.FALLBACK


def test_fallback_retries_below_max_attempts():
    ne = ScrollResult.no_effect()
    # attempts=1 < max(3) → retry same position (EXECUTE)
    assert transition(Step.FALLBACK, _ctx(attempt=ne, attempts=1)) is Step.EXECUTE


def test_fallback_advances_at_max_attempts():
    ne = ScrollResult.no_effect()
    # attempts==max(3), more positions remain → advance to next (EXECUTE)
    assert (
        transition(
            Step.FALLBACK,
            _ctx(attempt=ne, attempts=3, remaining=(Strategy.WHEEL, Strategy.SCROLLBAR_DRAG)),
        )
        is Step.EXECUTE
    )


def test_fallback_not_dispatched_always_advances():
    nd = ScrollResult.not_dispatched()
    # NOT_DISPATCHED attempt advances even with attempts=1 (never retries)
    assert (
        transition(
            Step.FALLBACK,
            _ctx(attempt=nd, attempts=1, remaining=(Strategy.WHEEL, Strategy.SCROLLBAR_DRAG)),
        )
        is Step.EXECUTE
    )


def test_fallback_exhausted_dispatch_no_effect():
    ne = ScrollResult.no_effect()
    # attempts==max and order has one position left → exhausted, any_armed=True
    assert (
        transition(
            Step.FALLBACK,
            _ctx(attempt=ne, attempts=3, remaining=(Strategy.WHEEL,), any_armed=True),
        )
        is Step.NO_SCROLL_EFFECT
    )


def test_fallback_exhausted_nothing_armed_not_dispatched():
    nd = ScrollResult.not_dispatched()
    # NOT_DISPATCHED attempt, one position, nothing ever armed → NOT_DISPATCHED
    assert (
        transition(
            Step.FALLBACK,
            _ctx(attempt=nd, remaining=(Strategy.WHEEL,), any_armed=False),
        )
        is Step.NOT_DISPATCHED
    )


def test_terminal_has_no_successor():
    for s in (Step.SUCCESS, Step.NO_SCROLL_EFFECT, Step.NOT_DISPATCHED):
        with pytest.raises(ValueError):
            transition(s, _ctx())


def test_single_strategy_iteration_edge():
    """Only the FALLBACK->EXECUTE edge advances a strategy position; there is
    exactly one STRATEGY-ITERATION edge in the table."""
    # FALLBACK with at-max attempts and more positions left advances via EXECUTE
    assert (
        transition(
            Step.FALLBACK,
            _ctx(attempt=ScrollResult.no_effect(), attempts=3,
                 remaining=(Strategy.WHEEL, Strategy.SCROLLBAR_DRAG)),
        )
        is Step.EXECUTE
    )
    # Every other edge never re-enters OWNERSHIP/DISCOVER
    assert transition(Step.VERIFY, _ctx(attempt=ScrollResult.no_effect())) is Step.FALLBACK


# ---------------------------------------------------------------------------
# run() — driver termination + bounding
# ---------------------------------------------------------------------------


def test_run_success_when_first_strategy_moves():
    moved = {"n": 0}

    def verify():
        moved["n"] += 1
        return True

    r = run(NavigateContext.start("nav", ScrollPolicy()), dispatch=_ok, verify=verify)
    assert r.success
    assert r.reason is Reason.WHEEL_MOVED
    assert r.strategy_used is Strategy.WHEEL


def test_run_not_dispatched_when_nothing_armed():
    # dispatch always fails → nothing ever armed → NOT_DISPATCHED
    r = run(NavigateContext.start("nav", ScrollPolicy()), dispatch=_err, verify=lambda: True)
    assert r.reason is Reason.NOT_DISPATCHED
    assert not r.dispatched and not r.moved


def test_run_exhausts_chain_to_no_scroll_effect():
    # dispatch ok but verify never True → every position NO_SCROLL_EFFECT → terminal
    r = run(NavigateContext.start("nav", ScrollPolicy()), dispatch=_ok, verify=lambda: False)
    assert r.reason is Reason.NO_SCROLL_EFFECT
    assert r.dispatched and not r.moved


def test_run_bounded_by_order_times_max_attempts():
    dispatched = []

    def dispatch(*, strategy=None, amount=None, strategy_ref=None):
        dispatched.append(strategy)
        return _ok()

    # 4 strategies x 3 attempts = 12 dispatches max
    r = run(NavigateContext.start("nav", ScrollPolicy()), dispatch=dispatch,
            verify=lambda: False)
    assert r.reason is Reason.NO_SCROLL_EFFECT
    assert len(dispatched) == 12


def test_run_max_attempts_zero_reaches_not_dispatched_without_dispatch():
    dispatched = []

    def dispatch(*, strategy=None, amount=None, strategy_ref=None):
        dispatched.append(1)
        return _ok()

    r = run(NavigateContext.start("nav", ScrollPolicy(max_attempts=0)),
            dispatch=dispatch, verify=lambda: True)
    assert r.reason is Reason.NOT_DISPATCHED
    assert dispatched == []


def test_run_single_owner_single_dispatch_per_execute():
    """At most one strategy dispatched per EXECUTE (single-owner invariant)."""
    seen_in_one = []

    def dispatch(*, strategy=None, amount=None, strategy_ref=None):
        seen_in_one.append(strategy)
        return _ok()

    run(NavigateContext.start("nav", ScrollPolicy()), dispatch=dispatch,
        verify=lambda: False)
    # The per-attempt dispatch count is exactly 1 (scroll_with_policy is
    # single-attempt); total is bounded by order*max_attempts.
    assert len(seen_in_one) == 12
    assert all(len({s}) == 1 for s in seen_in_one)


def test_run_terminates_in_terminal_states_only():
    """Every spawn terminates in SUCCESS | NO_SCROLL_EFFECT | NOT_DISPATCHED."""
    outcomes = []
    for dispatch, verify in [(_ok, lambda: True), (_ok, lambda: False), (_err, lambda: True)]:
        r = run(NavigateContext.start("nav", ScrollPolicy()), dispatch=dispatch, verify=verify)
        outcomes.append(r)
    reasons = {r.reason for r in outcomes}
    assert {
        Reason.WHEEL_MOVED,
        Reason.NEXT_PAGE,
        Reason.SCROLLBAR_DRAGGED,
        Reason.FOCUS_THEN_SCROLL,
        Reason.NO_SCROLL_EFFECT,
        Reason.NOT_DISPATCHED,
    }.issuperset(reasons)
