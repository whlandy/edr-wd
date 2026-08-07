"""
controller.py — PageController, the pure planner (PR3 / P0 review).
(docs/todo/scroll-and-paged-table-actions.md, enforceability item 3.)

`PageController` turns a `DetectionResult` + a `NavigationMode` into an
*ordered list of `PlannedAction`* (the executable commands). It NEVER touches
`ActionReceipt` — a receipt is the dispatcher's *result*, not a command
(Review P0-1 "Receipt is used as an executable command"). The execution layer
(`executor.DispatcherAdapter`) is the only thing that produces `ActionReceipt`.

One decision path: `plan(result, mode)` is the single planner entry point.

`NavigationMode` separates *movement directions* (MOVE_NEXT / MOVE_PREV) from
*jump targets* (RESET). NEXT/PREV describe "move one step in this direction
from the current position"; RESET is a state-transition target (return to the
first page / top). Mixing them in one enum would leak a weird API like
`plan(result, Direction.FIRST)` (Review P1 #2 — "FIRST is not a direction").

Primitive emission (grounding, item 3 "Mapping to current code"):

  * PAGINATED  MOVE_NEXT/MOVE_PREV → semantic click on the next / prev enabled
                owner (`gui.click`, A020).
  * LOAD_MORE  MOVE_NEXT           → semantic click on the load-more control
                (A020).
  * INFINITE   MOVE_NEXT           → wheel scroll of a signed click amount
                (`pointer.scroll`, A029) at the region.
  * FLAT       MOVE_NEXT           → wheel scroll (or scrollbar drag A028).
  * RESET      (any)               → previous-page click if a prev control
                exists, else a large wheel-up (A029).

An empty list means "nothing to dispatch" → maps to `NOT_DISPATCHED`.
The controller targets the single *enabled* `owner` of `DetectionResult`;
if the only next-control is disabled it returns `[]`.
"""

from __future__ import annotations

from enum import Enum
from typing import List, Sequence

from .actions import PlannedAction
from .detect import DetectedControl, DetectionResult, PageStructure


# Single-step wheel delta for an `A029 pointer.scroll` on an INFINITE/FLAT list.
DEFAULT_WHEEL_CLICKS = -3


class NavigationMode(Enum):
    """How to move on the current page structure.

    MOVE_NEXT / MOVE_PREV are *directional* steps (relative to the current
    position). RESET is a *target* (jump to first/top), not a direction.
    """

    MOVE_NEXT = "next"
    MOVE_PREV = "prev"
    RESET = "reset"


def _click_action(control: DetectedControl, action_id: str = "gui.click") -> PlannedAction:
    """Emit a semantic click command (A020) targeting the control."""
    return PlannedAction(
        action_id=action_id,
        action_code="A020",
        args={},
        target_ref={"target_id": control.target.target_id},
    )


def _wheel_action(clicks: int) -> PlannedAction:
    """Emit a `pointer.scroll` (A029) wheel command with a signed click count."""
    return PlannedAction(
        action_id="pointer.scroll",
        action_code="A029",
        args={"clicks": clicks},
    )


class PageController:
    """Pure planner. Holds no pointer/UI state — referentially transparent."""

    def plan(
        self,
        result: DetectionResult,
        mode: NavigationMode,
    ) -> List[PlannedAction]:
        """Plan the primitive for one navigation step in `mode`.

        Returns `[]` when there is nothing to dispatch: unknown structure for
        the requested mode, or no *enabled* owner control for a mode that
        needs one.
        """
        if mode is NavigationMode.MOVE_NEXT:
            return self._plan_next(result)
        if mode is NavigationMode.MOVE_PREV:
            return self._plan_prev(result)
        if mode is NavigationMode.RESET:
            return self._plan_first(result)
        raise ValueError(f"unknown navigation mode {mode!r}")

    # -- per-mode planners ---------------------------------------------------

    def _plan_next(self, result: DetectionResult) -> List[PlannedAction]:
        structure = result.structure
        if structure in (PageStructure.PAGINATED, PageStructure.LOAD_MORE):
            owner = result.owner
            if owner is None:
                return []
            return [_click_action(owner)]
        if structure in (PageStructure.INFINITE, PageStructure.FLAT):
            return [_wheel_action(DEFAULT_WHEEL_CLICKS)]
        return []

    def _plan_prev(self, result: DetectionResult) -> List[PlannedAction]:
        """Plan the primitive to move back one step in the same scroll/list.

        The PREV owner is chosen from `prev_controls`, NOT reusing the next
        control (Review P0 "prev executes next"). Falls back to an enabling
        wheel-up when there is no prev control.
        """
        if result.structure in (PageStructure.PAGINATED, PageStructure.LOAD_MORE):
            for ctrl in self._enabled(result.prev_controls):
                return [_click_action(ctrl)]
            # PAGINATED list with visible next but no prev => already at first
            # page; nothing to dispatch back to.
            return []
        if result.structure in (PageStructure.INFINITE, PageStructure.FLAT):
            # Reverse the wheel direction for a previous step.
            return [_wheel_action(-DEFAULT_WHEEL_CLICKS)]
        return []

    def _plan_first(self, result: DetectionResult) -> List[PlannedAction]:
        """Plan the primitive to return to the first page / top of the list."""
        if result.structure in (PageStructure.PAGINATED, PageStructure.LOAD_MORE):
            for ctrl in self._enabled(result.prev_controls):
                return [_click_action(ctrl)]
        # No previous/first control → wheel all the way up (single large A029).
        return [_wheel_action(abs(DEFAULT_WHEEL_CLICKS) * 3)]

    @staticmethod
    def _enabled(controls: Sequence[DetectedControl]) -> List[DetectedControl]:
        return [c for c in controls if c.enabled]


__all__ = ["PageController", "NavigationMode", "DEFAULT_WHEEL_CLICKS"]
