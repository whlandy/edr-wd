"""
page_table.py — pagination-only composite (T2).
(docs/todo/scroll-and-paged-table-actions.md, "T2: page_table".)

`page_table` is a *pagination-only* MCP/composite tool. Its contract is
strictly narrower than the general `scroll_region` composite:

  * It dispatches ONLY semantic pagination clicks (`gui.click`, A020).
  * It NEVER emits `pointer.scroll` (A029) or `pointer.drag` (A028) — for a
    paged table surface, wheel/drag have no meaning and must not be the
    backing mechanism.
  * It uses `PageDetector` once to confirm the surface is `PAGINATED` or
    `LOAD_MORE`; every other structure (`FLAT`, `INFINITE`, `TREE_LAZY`)
    returns `NOT_DISPATCHED`.
  * A disabled next/prev control or the absence of an enabled owner also
    returns `NOT_DISPATCHED` (nothing is armed).
  * Movement is verified with `PageVerifier`/`Observer` before reporting
    `moved=True`; verification churn never counts as movement.
  * Each call turns AT MOST one page. Draining all pages is a caller loop, not
    this tool's job.

Role split (consistent with the rest of target/scroll):

  * `PageTableController` — PURE planner. Turns a `DetectionResult` +
    direction into an ordered list of `PlannedAction` of A020 semantic clicks,
    or `[]` (→ `NOT_DISPATCHED`). It never touches `ActionReceipt` and never
    fabricates a wheel/drag command.
  * `run_page_table(...)` — the thin production/side-effecting facade that
    composes backend snapshot → detect → plan → real dispatcher (via
    `ScrollExecutor`/`DispatcherAdapter`) → bounded verify, and returns a
    `ScrollResult`-shaped dict/`ScrollResult`.

Direction vocabulary (T2 signature): "next" | "prev" | "first". These map onto
the existing `NavigationMode` (MOVE_NEXT / MOVE_PREV / RESET) for the semantic
click planner.
"""

from __future__ import annotations

from typing import List, Optional

try:
    from ..observations.models import ObservationSnapshot
except ImportError:
    from observations.models import ObservationSnapshot

from .actions import PlannedAction
from .controller import NavigationMode, _click_action
from .detect import DetectedControl, DetectionResult, PageChange, PageStructure
from .executor import ScrollExecutor
from .observer import Observer
from .results import Reason, ScrollResult
from .verifier import PageVerifier

# Structures that are NOT paged-tables — page_table must not touch them.
_NON_PAGED = frozenset({
    PageStructure.FLAT,
    PageStructure.INFINITE,
    PageStructure.TREE_LAZY,
})

# Direction vocabulary accepted by page_table (T2 signature).
VALID_DIRECTIONS = ("next", "prev", "first")


def _mode_for(direction: str) -> NavigationMode:
    """Map the T2 string direction onto the shared NavigationMode."""

    return {
        "next": NavigationMode.MOVE_NEXT,
        "prev": NavigationMode.MOVE_PREV,
        "first": NavigationMode.RESET,
    }[direction]


class PageTableController:
    """PURE planner emitting ONLY semantic pagination clicks (A020).

    `plan(result, direction)` returns an ordered list of `PlannedAction`
    (all `gui.click` / A020) that turn one page in `direction`, or `[]` when
    there is nothing to dispatch (→ the caller reports `NOT_DISPATCHED`).

    No A028/A029 is ever produced here: the only emission path is the
    semantic `_click_action` helper. Anything that is not a paged surface, or
    that has no *enabled* owner control for the requested direction, yields an
    empty plan.
    """

    def plan(self, result: DetectionResult, direction: str) -> List[PlannedAction]:
        if direction not in VALID_DIRECTIONS:
            raise ValueError(
                f"unknown page_table direction {direction!r}; "
                f"expected one of {VALID_DIRECTIONS}"
            )
        # Only paged surfaces are in scope (T2: FLAT/INFINITE/TREE_LAZY →
        # NOT_DISPATCHED, never a wheel/drag fallback).
        if result.structure not in (PageStructure.PAGINATED, PageStructure.LOAD_MORE):
            return []

        mode = _mode_for(direction)
        if mode is NavigationMode.MOVE_NEXT:
            owner = result.owner
            if owner is None:
                return []
            return [_click_action(owner)]
        # prev / first both target the enabled previous-page control; a paged
        # surface already at the first page has no prev control armed.
        for ctrl in self._enabled(result.prev_controls):
            return [_click_action(ctrl)]
        return []

    @staticmethod
    def _enabled(controls) -> List[DetectedControl]:
        return [c for c in controls if c.enabled]


class PageTableVerifier:
    """Bounded verification for ONE page turn (T2 acceptance: churn ≠ move).

    Uses `PageVerifier.page_changed` (the PURE tri-state judge) over the
    before/after snapshot pair, polling with backoff up to `max_attempts`. Only
    a genuine `CHANGED` verdict (a real digest change in the owner region)
    counts as movement — transient churn that never stabilises returns
    `NO_SCROLL_EFFECT`, never `moved=True`.
    """

    def __init__(
        self,
        verifier: Optional[PageVerifier] = None,
        observer: Optional[Observer] = None,
        threshold: float = 0.0,
        backoff_ms: int = 0,
    ) -> None:
        self.verifier = verifier or PageVerifier()
        self.observer = observer
        self.threshold = threshold
        self.backoff_ms = backoff_ms

    def verify_move(
        self,
        before: ObservationSnapshot,
        max_attempts: int,
        region: tuple[int, int, int, int] | None = None,
    ) -> bool:
        """Poll for a genuine content change after the dispatch.

        Returns True only when `PageVerifier.page_changed` reports `CHANGED`
        within `max_attempts` polls. Returns False on UNCHANGED/UNCERTAIN —
        churn that never resolves is never a move.
        """
        for attempt in range(max(1, max_attempts)):
            if self.observer is None:
                raise ValueError("PageTableVerifier requires an observer")
            after = self.observer.snapshot()
            change = self.verifier.page_changed(
                before, after, self.threshold, region=region
            )
            if change is PageChange.CHANGED:
                return True
            if self.backoff_ms > 0 and attempt < (max(1, max_attempts) - 1):
                self._sleep(self.backoff_ms / 1000.0)
        return False

    @staticmethod
    def _sleep(seconds: float) -> None:
        import time

        time.sleep(seconds)


def step_page_table(
    result: DetectionResult,
    direction: str,
    *,
    before: ObservationSnapshot,
    executor: ScrollExecutor,
    observer: Observer,
    verifier: Optional[PageVerifier] = None,
    threshold: float = 0.0,
    verify: bool = True,
    max_attempts: Optional[int] = None,
) -> ScrollResult:
    """Turn one page via the pure planner + real dispatcher + verify.

    This is the per-call body of `page_table`. It is split out (rather than
    buried in `run_page_table`) so it can be unit-tested without a backend.

    Flow:
      * `PageTableController.plan` → `[]` ⇒ `NOT_DISPATCHED` (disabled
        next/prev, no owner, or a non-paged structure).
      * Dispatch the plan through the real adapter; no receipt `ok` ⇒
        `NOT_DISPATCHED` (nothing was armed).
      * `verify=False`: do not poll; report the dispatch honestly as
        `NO_SCROLL_EFFECT` with `moved=False` (we can never claim movement we
        did not observe).
      * `verify=True`: bounded poll via `PageVerifier` until a genuine
        `CHANGED`; only then report `success` (NEXT_PAGE). Exhausting the
        bound without a real change ⇒ `NO_SCROLL_EFFECT`.
    """
    plan = PageTableController().plan(result, direction)
    if not plan:
        return ScrollResult.not_dispatched()

    # Execute through the real executor/adapter. Dispatch success is never
    # movement (item 1); we only learn movement by verifying.
    receipts = executor.execute_plan(plan)
    if not any(r.ok for r in receipts):
        return ScrollResult.not_dispatched()

    if not verify:
        # We cannot substantiate movement without observing it. Honest
        # dispatched-but-unverified outcome (see design open question; until
        # `UNVERIFIED` is added, NO_SCROLL_EFFECT is the truthful result).
        return ScrollResult.no_effect()

    # Region-scope verification to the owner control's bounds (f5): unrelated
    # window churn outside the target area must not count as a move.
    region = None
    owner_rect = _owner_rect(result)
    if owner_rect is not None:
        region = owner_rect

    watcher = PageTableVerifier(verifier=verifier, observer=observer,
                                threshold=threshold)
    if watcher.verify_move(before, max_attempts or 1, region=region):
        return ScrollResult.success_result(
            _strategy_for(result), reason=Reason.NEXT_PAGE
        )
    return ScrollResult.no_effect()


def _owner_rect(result: DetectionResult) -> Optional[tuple]:
    """The rect of the enabled next/load-more owner, for region-scoped verify."""
    owner = result.owner
    if owner is not None and owner.target.rect is not None:
        return owner.target.rect
    return None


def _strategy_for(result: DetectionResult):
    """Attribution strategy for a paged surface (PAGINATION)."""
    from .results import Strategy

    return Strategy.PAGINATION


__all__ = [
    "PageTableController",
    "PageTableVerifier",
    "step_page_table",
    "VALID_DIRECTIONS",
    "_NON_PAGED",
]
