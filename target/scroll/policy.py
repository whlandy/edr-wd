"""
policy.py — ScrollPolicy (bound) + scroll_with_policy (single-attempt executor).
(docs/todo/scroll-and-paged-table-actions.md, enforceability item 2; PR2.)

`ScrollPolicy` carries **all** bounds with the caller — there is no module-level
integer cap. The two distinct bounds are:

  * `max_attempts` — the per-position attempt budget. A "position" is one
    `Strategy` in `strategy_order`. The machine (item 4 / `state_machine.py`)
    drives positions via the single `FALLBACK -> EXECUTE` edge; `EXECUTE`
    calls `scroll_with_policy` once per position.
  * total-dispatches bound — derived, `len(strategy_order) * max_attempts`
    (item 4's formula). Never stored as a separate constant.

`scroll_with_policy` is the **single-attempt** strategy executor, NOT a loop.
One call = one `EXECUTE` = one strategy dispatch + one `verify()` for the
current position in `strategy_order`. Termination is owned by the machine, not
by this function:

  * receipt `ok=False` (strategy could not be armed) ⇒
    `ScrollResult.not_dispatched()` — `NOT_DISPATCHED`.
  * receipt armed + `verify()` True ⇒ `ScrollResult.success_result(strategy)`
    with the PR1-pinned `strategy_used` attribution.
  * receipt armed + `verify()` False ⇒ `ScrollResult.no_effect()` —
    `NO_SCROLL_EFFECT`. NOT a terminal return; it drives the machine's
    `FALLBACK`.

Signature order is pinned here as `(policy, verify, dispatch)` (the design
resolves the policy/verify/dispatch vs. policy/dispatch/verify ambiguity in
favor of `(policy, verify, dispatch)`); every call site uses keyword args so
order cannot silently drift.

PR2 stub boundary: only `Strategy.WHEEL` is exercised for real (the `FLAT ->
WHEEL` hard-coded `CLASSIFY`/`OWNERSHIP` mapping); `PAGINATION` and
`FOCUS_THEN_SCROLL` resolve to "nothing armed" via the injected `dispatch`
until PR3 wires detection. No dead strategy branches are shipped here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional, Protocol

from action_dispatcher.receipts import ActionReceipt

from .results import ScrollResult, Strategy


# Injected verify callback: returns whether content actually moved between the
# before/after snapshots (PR1's `Observer.content_changed`).
VerifyFn = Callable[[], bool]

# Injected dispatch callback: arms the primitive for `strategy` and returns an
# `ActionReceipt` whose `ok` signals dispatch ONLY, never movement.
# `amount` is the optional signed click/step count (A029 `clicks`).
DispatchFn = Callable[..., ActionReceipt]


@dataclass(frozen=True)
class ScrollPolicy:
    """Bounds that travel with the caller. Frozen so a shared policy cannot be
    mutated mid-run and so it is safely reusable across `NavigatesContext`s.

    Args:
        strategy_order: the ordered strategy list for the current surface. The
            static PR2 default is escalation order (wheel → scrollbar drag →
            focus-then-scroll → pagination). Detection (`DetectionResult`) may
            reorder this in PR3's `OWNERSHIP` phase; the machine never
            re-decides order inside `transition`.
        max_attempts: per-position attempt budget. `0` ⇒ "nothing ever armed"
            (the NOT_DISPATCHED path, no dispatch). Default `3` per item 2.
            An instrumented policy with `max_attempts >= len(strategy_order)`
            exercises the full chain (the default cap shadows deeper
            strategies).
        backoff_initial_ms: optional initial backoff between attempts (ms).
        backoff_factor: exponential factor; `None`/`1` ⇒ constant backoff.
    """

    strategy_order: tuple[Strategy, ...] = (
        Strategy.WHEEL,
        Strategy.SCROLLBAR_DRAG,
        Strategy.FOCUS_THEN_SCROLL,
        Strategy.PAGINATION,
    )
    max_attempts: int = 3
    backoff_initial_ms: int = 0
    backoff_factor: Optional[float] = None

    def __post_init__(self) -> None:
        if self.max_attempts < 0:
            raise ValueError("max_attempts must be >= 0")
        if not self.strategy_order:
            raise ValueError("strategy_order must not be empty")
        if self.backoff_initial_ms < 0:
            raise ValueError("backoff_initial_ms must be >= 0")

    @property
    def total_dispatch_bound(self) -> int:
        """Derived total-dispatches upper bound (item 4 formula)."""
        return len(self.strategy_order) * self.max_attempts

    def backoff_ms(self, attempt: int) -> int:
        """Backoff delay (ms) before attempt `attempt` (0-based). Linear when
        `backoff_factor` is None; exponential otherwise."""
        if attempt <= 0 or self.backoff_initial_ms <= 0:
            return 0
        if self.backoff_factor:
            return int(self.backoff_initial_ms * (self.backoff_factor ** (attempt - 1)))
        return self.backoff_initial_ms * attempt


def scroll_with_policy(
    *,
    policy: ScrollPolicy,
    verify: VerifyFn,
    dispatch: DispatchFn,
    strategy: Strategy,
    amount: Optional[int] = None,
    strategy_ref: Optional[object] = None,
) -> ScrollResult:
    """Single-attempt strategy executor (item 2 / item 4 EXECUTE body).

    Exactly one dispatch + one verify for the *current* strategy position.
    The `max_attempts` bound is applied by the caller (the machine), never
    re-implemented here — this function performs a single attempt regardless
    of `policy.max_attempts`.

    Note: a single `pointer.scroll` (A029) with a signed `clicks` is one
    dispatch even for a multi-step wheel move; `scroll_with_policy` never
    re-loops it (per `target/automation/base.py`).
    """
    # A policy with max_attempts==0 means nothing is ever armed.
    if policy.max_attempts == 0:
        return ScrollResult.not_dispatched()

    receipt = dispatch(strategy=strategy, amount=amount, strategy_ref=strategy_ref)
    if not receipt.ok:
        return ScrollResult.not_dispatched()

    moved = bool(verify())
    if not moved:
        # Dispatched-but-unmoved: drives FALLBACK (not terminal from here).
        return ScrollResult.no_effect()

    return ScrollResult.success_result(strategy)


__all__ = ["ScrollPolicy", "scroll_with_policy", "VerifyFn", "DispatchFn"]
