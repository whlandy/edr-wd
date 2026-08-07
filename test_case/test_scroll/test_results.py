"""
PR1 acceptance — ScrollResult / Reason / Strategy contract
(docs/todo/scroll-and-paged-table-actions.md, enforceability item 1).

White-box constructor tests for every item-1 invariant.
"""

from __future__ import annotations

import pytest


from scroll.results import Reason, ScrollResult, Strategy


# ---------------------------------------------------------------------------
# Success / outcome predicates
# ---------------------------------------------------------------------------


def test_success_requires_dispatched_and_moved():
    r = ScrollResult(dispatched=True, moved=True, reason=Reason.WHEEL_MOVED,
                     strategy_used=Strategy.WHEEL)
    assert r.success is True
    assert r.outcome == "moved"


def test_dispatched_but_unmoved_is_not_success():
    r = ScrollResult(dispatched=True, moved=False,
                     reason=Reason.NO_SCROLL_EFFECT)
    assert r.success is False
    assert r.outcome == "dispatched_only"


def test_not_dispatched_outcome():
    r = ScrollResult(dispatched=False, moved=False,
                     reason=Reason.NOT_DISPATCHED)
    assert r.success is False
    assert r.outcome == "not_dispatched"


# ---------------------------------------------------------------------------
# Invariants raised in __post_init__
# ---------------------------------------------------------------------------


def test_moved_requires_dispatched():
    with pytest.raises(ValueError):
        ScrollResult(dispatched=False, moved=True, reason=Reason.WHEEL_MOVED)


def test_no_scroll_effect_requires_moved_false():
    with pytest.raises(ValueError):
        ScrollResult(dispatched=True, moved=True,
                     reason=Reason.NO_SCROLL_EFFECT)


def test_not_dispatched_requires_dispatched_false():
    with pytest.raises(ValueError):
        ScrollResult(dispatched=True, moved=False,
                     reason=Reason.NOT_DISPATCHED)


def test_mechanism_reason_requires_moved_true():
    # A WHEEL_MOVED outcome cannot be unmoved.
    with pytest.raises(ValueError):
        ScrollResult(dispatched=True, moved=False, reason=Reason.WHEEL_MOVED)


def test_strategy_used_requires_moved_true():
    with pytest.raises(ValueError):
        ScrollResult(dispatched=True, moved=False,
                     reason=Reason.NO_SCROLL_EFFECT, strategy_used=Strategy.WHEEL)


# ---------------------------------------------------------------------------
# Success constructor + reason attribution
# ---------------------------------------------------------------------------


def test_success_result_sets_strategy_used_and_mechanism_reason():
    r = ScrollResult.success_result(Strategy.WHEEL)
    assert r.dispatched and r.moved
    assert r.strategy_used is Strategy.WHEEL
    assert r.reason is Reason.WHEEL_MOVED
    assert r.success


def test_success_result_custom_reason_override():
    r = ScrollResult.success_result(Strategy.PAGINATION, reason=Reason.NEXT_PAGE)
    assert r.reason is Reason.NEXT_PAGE
    assert r.strategy_used is Strategy.PAGINATION


def test_success_never_stored_derived_predicate():
    # `SUCCESS` is not an enum member (derived, never stored).
    names = {m.name for m in Reason}
    assert "SUCCESS" not in names


# ---------------------------------------------------------------------------
# Strategy.success_reason attribution table
# ---------------------------------------------------------------------------


def test_strategy_success_reason_table():
    assert Strategy.WHEEL.success_reason() is Reason.WHEEL_MOVED
    assert Strategy.SCROLLBAR_DRAG.success_reason() is Reason.SCROLLBAR_DRAGGED
    assert Strategy.FOCUS_THEN_SCROLL.success_reason() is Reason.FOCUS_THEN_SCROLL
    assert Strategy.PAGINATION.success_reason() is Reason.NEXT_PAGE


def test_frozen_and_serialization():
    r = ScrollResult(dispatched=True, moved=True, reason=Reason.WHEEL_MOVED,
                     strategy_used=Strategy.WHEEL)
    with pytest.raises(Exception):
        r.dispatched = False  # frozen
    d = r.to_dict()
    assert d["success"] is True
    assert d["reason"] == "wheel_moved"
    assert d["strategy_used"] == "wheel"


def test_no_effect_and_not_dispatched_helpers():
    ne = ScrollResult.no_effect()
    assert (ne.dispatched, ne.moved, ne.reason) == (True, False, Reason.NO_SCROLL_EFFECT)
    nd = ScrollResult.not_dispatched()
    assert (nd.dispatched, nd.moved, nd.reason) == (False, False, Reason.NOT_DISPATCHED)
