"""
actions.py — PlannedAction (the executable command) + DispatcherAdapter
(P0-1: fix the Receipt/Command confusion).

`ActionReceipt` (target/action_dispatcher/receipts.py) is the **result
envelope returned AFTER dispatch** — it is not a command and must never be
fabricated as if it were. The dispatcher's real entry point is:

    dispatch(action_id, *, action_code=None, args=None, target_ref=None,
             request_id=None, ...) -> ActionReceipt

`PlannedAction` is the *command* a pure planner (PageController) produces for
one primitive step. `DispatcherAdapter` is the single bridge that turns a
`PlannedAction` into a real `dispatch(...)` call and returns the real
`ActionReceipt`. Keeping this adapter as the only place that touches the
dispatcher keeps the pure Detector/Controller/Verifier roles free of any
dispatcher dependency (referential transparency, item 3).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional, Protocol

from action_dispatcher.receipts import ActionReceipt

# Real dispatcher signature: dispatch(action_id, *, action_code, args,
# target_ref, request_id, ...) -> ActionReceipt (target/action_dispatcher/
# dispatch.py). We only exercise the subset the scroll system needs.
DispatchCallable = Callable[..., ActionReceipt]


@dataclass(frozen=True, kw_only=True)
class PlannedAction:
    """One executable command produced by a pure planner.

    Fields mirror the real `dispatch()` call shape so the adapter is a trivial
    mechanical bridge:

      * action_id    — catalog id, e.g. "gui.click" (A020) / "pointer.scroll"
                       (A029) / "pointer.drag" (A028).
      * action_code  — the stable catalog code (A020/A028/A029).
      * args         — backend args, e.g. {"clicks": -3} for a wheel step.
      * target_ref   — the resolved target reference the execution layer
                       resolves (e.g. {"snapshot_id":..., "target_id":...}).
    """

    # kw_only protects against positional mis-binding: `args` is the natural
    # second field to type as `"A020"` accidentally. All construction must be
    # keyword (Review P1 "actions.py 字段顺序风险").
    action_id: str
    args: Mapping[str, Any]
    # Optional, not "" — an empty string would mask a missing code and only
    # surface at dispatch time as `None`. Callers that need a code pass it
    # explicitly.
    action_code: Optional[str] = None
    target_ref: Optional[Mapping[str, Any]] = None

    def __post_init__(self) -> None:
        if not self.action_id:
            raise ValueError("PlannedAction requires a non-empty action_id")

    def dispatch_kwargs(self) -> dict:
        """The kwargs to pass straight into the real `dispatch()`."""
        return {
            "action_code": self.action_code,
            "args": dict(self.args),
            "target_ref": dict(self.target_ref) if self.target_ref else None,
        }


def _default_dispatcher(action_id: str, **kwargs: Any) -> ActionReceipt:
    """Default dispatcher: call the repository's real `dispatch()`."""
    from action_dispatcher.dispatch import dispatch as _real

    return _real(action_id, **kwargs)


class DispatcherAdapter:
    """Bridges `PlannedAction` -> real `dispatch()` -> `ActionReceipt`.

    Injectable so tests can substitute a fake dispatcher; production uses the
    real `dispatch()` from `target/action_dispatcher/dispatch.py`.
    """

    def __init__(self, dispatch: Optional[DispatchCallable] = None) -> None:
        self._dispatch = dispatch or _default_dispatcher

    def execute(self, action: PlannedAction) -> ActionReceipt:
        """Execute one planned command; return the real dispatch receipt."""
        return self._dispatch(action.action_id, **action.dispatch_kwargs())

    def execute_all(self, actions: list[PlannedAction]) -> list[ActionReceipt]:
        """Execute an ordered plan; return receipts in the same order. The
        caller decides success from each receipt's `ok` (dispatch-only, never
        movement)."""
        return [self.execute(a) for a in actions]


__all__ = ["PlannedAction", "DispatcherAdapter", "DispatchCallable"]
