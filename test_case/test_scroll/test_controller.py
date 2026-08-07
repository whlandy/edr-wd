"""
PR3 acceptance + P0 review — PageController pure planner emits PlannedAction
(docs/todo/scroll-and-paged-table-actions.md, enforceability item 3;
Review P0 "Receipt is used as an executable command").

The planner produces `PlannedAction` (executable commands), never
`ActionReceipt` (results). One entry point: `plan(result, direction)` with
NEXT / PREV / FIRST as the only directions.
"""

from __future__ import annotations

from observations.models import Target
from scroll.actions import PlannedAction
from scroll.controller import NavigationMode, PageController
from scroll.detect import DetectedControl, DetectionResult, PageStructure


def _control(target_id="T0001", title="btn"):
    return Target(target_id=target_id, kind="control", process_name="X",
                  pid=1, native_window_id="w", title=title,
                  control_type="Button", automation_id="nextPageButton")


def _owner(target_id="T0001", enabled=True):
    return DetectedControl(target=_control(target_id), enabled=enabled)


def _prev(target_id="T0000", enabled=True):
    return DetectedControl(
        target=_control(target_id, "prev"), enabled=enabled)


def _paginated(enabled=True):
    return DetectionResult(
        structure=PageStructure.PAGINATED,
        next_controls=(_owner(enabled=enabled),),
        confidence=0.95,
    )


def _load_more():
    return DetectionResult(
        structure=PageStructure.LOAD_MORE,
        next_controls=(_owner(),),
        confidence=0.9,
    )


def _infinite():
    return DetectionResult(structure=PageStructure.INFINITE, confidence=0.8)


def _flat():
    return DetectionResult(structure=PageStructure.FLAT, confidence=0.0)


def _codes(plan):
    return [a.action_code for a in plan]


# ---------------------------------------------------------------------------


def test_plan_next_paginated_emits_single_click():
    plan = PageController().plan(_paginated(), NavigationMode.MOVE_NEXT)
    assert len(plan) == 1
    assert isinstance(plan[0], PlannedAction)
    assert _codes(plan) == ["A020"]
    assert plan[0].action_id == "gui.click"
    assert plan[0].target_ref == {"target_id": "T0001"}


def test_plan_next_load_more_emits_single_click():
    assert _codes(PageController().plan(_load_more(), NavigationMode.MOVE_NEXT)) == ["A020"]


def test_plan_next_infinite_emits_wheel():
    plan = PageController().plan(_infinite(), NavigationMode.MOVE_NEXT)
    assert _codes(plan) == ["A029"]
    assert plan[0].action_id == "pointer.scroll"
    assert plan[0].args["clicks"] < 0


def test_plan_next_flat_emits_wheel():
    assert _codes(PageController().plan(_flat(), NavigationMode.MOVE_NEXT)) == ["A029"]


def test_plan_next_paginated_disabled_owner_returns_empty():
    # Disabled owner → nothing to dispatch → [] (→ NOT_DISPATCHED)
    assert PageController().plan(_paginated(enabled=False), NavigationMode.MOVE_NEXT) == []


# -- PREV uses a dedicated prev owner (Review P0 "prev executes next") -------


def test_plan_prev_uses_prev_control_not_next():
    result = DetectionResult(
        structure=PageStructure.PAGINATED,
        next_controls=(_owner("T0001"),),
        prev_controls=(_prev("T0042"),),
        confidence=0.95,
    )
    plan = PageController().plan(result, NavigationMode.MOVE_PREV)
    assert len(plan) == 1
    assert plan[0].target_ref == {"target_id": "T0042"}  # NB: not T0001
    assert _codes(plan) == ["A020"]


def test_plan_prev_no_prev_control_returns_empty_for_paginated():
    """PAGINATED with a next but no prev = already at first page."""
    assert PageController().plan(_paginated(), NavigationMode.MOVE_PREV) == []


def test_plan_prev_infinite_reverses_wheel():
    plan = PageController().plan(_infinite(), NavigationMode.MOVE_PREV)
    assert _codes(plan) == ["A029"]
    assert plan[0].args["clicks"] > 0  # reverse direction


def test_plan_prev_disabled_prev_not_used():
    result = DetectionResult(
        structure=PageStructure.PAGINATED,
        next_controls=(_owner("T0001"),),
        prev_controls=(_prev("T0042", enabled=False),),
        confidence=0.95,
    )
    assert PageController().plan(result, NavigationMode.MOVE_PREV) == []


# -- FIRST --------------------------------------------------------------------


def test_plan_first_with_prev_control_emits_click():
    result = DetectionResult(
        structure=PageStructure.PAGINATED,
        next_controls=(_owner(),),
        prev_controls=(_prev("T0042"),),
        confidence=0.95,
    )
    plan = PageController().plan(result, NavigationMode.RESET)
    assert _codes(plan) == ["A020"]
    assert plan[0].target_ref == {"target_id": "T0042"}


def test_plan_first_no_prev_emits_big_wheel():
    plan = PageController().plan(_paginated(), NavigationMode.RESET)
    assert _codes(plan) == ["A029"]


# -- no accidental pagination for scrollbar/flat ------------------------------


def test_never_misclassifies_scrollbar_handle_as_paginated():
    """A scrollbar-drag handle / FLAT region must plan WHEEL (A029), never a
    PAGINATION click (A020) — guards the item-5 slider/handle regression."""
    assert _codes(PageController().plan(_flat(), NavigationMode.MOVE_NEXT)) == ["A029"]
    assert _codes(PageController().plan(_infinite(), NavigationMode.MOVE_NEXT)) == ["A029"]


def test_unknown_direction_raises():
    import pytest
    with pytest.raises(ValueError):
        PageController().plan(_paginated(), "sideways")  # type: ignore[arg-type]
