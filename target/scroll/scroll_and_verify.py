"""
scroll_and_verify.py — single-strategy composite (PR1, enforcement item 1/6).

This is the PR1 composite: snapshot-before → dispatch exactly one primitive →
snapshot-after → compute moved → return a `ScrollResult`. It establishes the
"dispatch vs. moved" contract that PR2's `scroll_with_policy` / state machine
and the T1..T4 tool wrappers all build on.

Scope (explicitly NOT here — that is PR2/PR3):
  * no `ScrollPolicy`, no strategy chain / state machine,
  * no `PageDetector` / `PageController` / `PageVerifier`,
  * no pagination logic.

`moved` is ONLY ever produced by the `Observer` — never from a coordinate
delta or a raw `ActionReceipt.ok`. `dispatched` is derived from the receipt's
`ok` (`target/action_dispatcher/receipts.py`).

`dispatch` is an injectable callable so the composite is testable without an
MCP transport and reusable across strategies:
    def dispatch(*, strategy: Strategy, amount: int | None = None) -> ActionReceipt
The PR1 single-strategy caller ignores the loop/`amount` surface; the shape is
pinned now so PR2's `scroll_with_policy` and T1..T4 share one call signature.
"""

from __future__ import annotations

from typing import Callable, Optional, Protocol

from action_dispatcher.receipts import ActionReceipt
from observations.models import ObservationSnapshot

from .observer import DiffStrategy, Observer
from .results import Reason, ScrollResult, Strategy


class DispatchFn(Protocol):
    """Injected dispatcher. Returns an `ActionReceipt` whose `ok` signals
    dispatch only (never movement)."""

    def __call__(
        self,
        *,
        strategy: Strategy,
        amount: Optional[int] = None,
        strategy_ref: Optional[object] = None,
    ) -> ActionReceipt: ...


def scroll_and_verify(
    *,
    dispatch: DispatchFn,
    observer: Observer,
    strategy: Strategy,
    amount: Optional[int] = None,
    verify: bool = True,
    strategy_ref: Optional[object] = None,
) -> ScrollResult:
    """Single-strategy composite: dispatch once, verify once, return the
    contract.

    Args:
        dispatch: callable dispatching the primitive and returning an
            `ActionReceipt` (dispatch-injected; testable).
        observer: the PR1 `Observer`; `moved` comes only from here.
        strategy: the strategy/primitive to dispatch (e.g. `Strategy.WHEEL`).
        amount: optional signed click/step count passed to `dispatch`
            (multi-clicks stay a single A029 dispatch per `automation.base`).
        verify: if `False`, skip the after-snapshot and force `moved=False`
            (never fabricates movement — design item: opt-out of a meaningful
            result; a caller relying on `moved=True` must leave it `True`).
        strategy_ref: optional opaque reference (e.g. a resolved `Target`)
            carried for attribution/logging; PR2's `NavigateContext` slots it.

    Returns:
        `ScrollResult` per the item-1 mapping:
          * receipt.ok is False ⇒ dispatched=False, reason=NOT_DISPATCHED
          * ok is True, moved is False ⇒ dispatched=True, moved=False,
            reason=NO_SCROLL_EFFECT
          * ok is True, moved is True ⇒ success, reason = mechanism member for
            `strategy`, strategy_used=strategy

    R0 invariant: dispatching is byte-identical to today's raw primitive path;
    the only addition is the Observer's verify *after* dispatch. A returned
    `ScrollResult` can never cause extra input.
    """
    # Actually snapshot BEFORE dispatch.
    before: ObservationSnapshot | None = None
    if verify:
        before = observer.snapshot()

    receipt = dispatch(strategy=strategy, amount=amount, strategy_ref=strategy_ref)

    if not receipt.ok:
        # Nothing was armed → NOT_DISPATCHED (item-1 mapping).
        return ScrollResult.not_dispatched()

    # A receipt armed. If we skip verification, moved is forced False and the
    # only honest outcome is NO_SCROLL_EFFECT (we cannot prove movement).
    if not verify:
        return ScrollResult.no_effect()

    assert before is not None  # verify=True set it above
    after = observer.snapshot()
    moved = observer.content_changed(
        before, after, strategy=DiffStrategy.TREE_DIGEST
    )
    if not moved:
        return ScrollResult.no_effect()

    return ScrollResult.success_result(strategy)


__all__ = ["scroll_and_verify", "DispatchFn"]
