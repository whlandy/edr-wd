"""
executor.py — ScrollExecutor, the single execution layer (P0 review).

Separates Planner from Executor:

    PageController  (pure)   --DetectionResult + Direction-->  PlannedAction
    DispatcherAdapter        --PlannedAction--> dispatch() --> ActionReceipt
    ScrollExecutor  (io)     --owns adapter only today-->

`ActionReceipt` is produced here and ONLY here for the scroll system. The
executor is where the real dispatcher (via `DispatcherAdapter`) is called and
where the "dispatch is not movement" boundary lives: a receipt is treated as
"armed" iff `receipt.ok`, and is never proof of *movement* by itself.

NOTE (review P1 #1): do NOT keep growing this class. It is intentionally just
an ActionRunner today (plan -> receipts). Retry / polling / stability belong in
separate modules (execution/retry.py, execution/polling.py) when they land —
not as more methods here, or it becomes a God class.
"""

from __future__ import annotations

from typing import Optional

from action_dispatcher.receipts import ActionReceipt

from .actions import DispatcherAdapter, PlannedAction


class ScrollExecutor:
    """Executes a `PlannedAction` plan through the real dispatcher and yields
    the raw receipts. Holds no navigation state (that is the machine's job);
    it is the input/output boundary of the scroll system."""

    def __init__(
        self,
        adapter: Optional[DispatcherAdapter] = None,
    ) -> None:
        self.adapter = adapter or DispatcherAdapter()

    def execute(self, action: PlannedAction) -> ActionReceipt:
        """Dispatch one planned command; return the real dispatcher receipt."""
        return self.adapter.execute(action)

    def execute_plan(self, plan: list[PlannedAction]) -> list[ActionReceipt]:
        """Dispatch an ordered plan; return receipts in the same order.

        The caller treats a receipt as "armed" iff `receipt.ok`; a receipt is
        never proof of *movement* (that requires verification, item 1)."""
        return self.adapter.execute_all(plan)


__all__ = ["ScrollExecutor"]
