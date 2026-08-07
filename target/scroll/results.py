"""
results.py — ScrollResult contract (PR1, enforceability item 1).

The core decision of this whole feature: a composite scroll action must
separate "the primitive was dispatched" from "the content actually moved".
A bare low-level `ok=true` (an `ActionReceipt` from
`target/action_dispatcher/receipts.py`) only ever means *dispatched* — it
never means *moved*. Verification of real movement is the composite layer's
job, and `ScrollResult` is the frozen envelope that carries both signals
without letting a caller conflate them.

Design notes (docs/todo/scroll-and-paged-table-actions.md, item 1):

  * `Reason` is a typed enum of *outcomes*. There is deliberately **no
    `SUCCESS` member** — success is derived as `dispatched and moved`,
    never stored, so a caller cannot pattern-match a hand-placed "success"
    marker.
  * `Strategy` (used in `strategy_used`) is declared here rather than in the
    PR2 policy module so that the frozen dataclass field does not churn when
    PR2 introduces `ScrollPolicy`. It is the set of composable scroll
    strategies (item 2), each mapping onto a catalog primitive or composite.
  * `strategy_used` is a separate attribution field (design preference: "a
    separate `strategy_used` field ... avoids overloading `reason`'s outcome
    axis and keeps the item-1 enum stable"). `reason` stays on the outcome
    axis (`WHEEL_MOVED`, `SCROLLBAR_DRAGGED`, `NO_SCROLL_EFFECT`,
    `NOT_DISPATCHED`, ...); `strategy_used` records *which* strategy the
    composite actually executed when it moved.

Invariants enforced in `__post_init__` (each backed by a unit test):

  * `moved ⇒ dispatched`  — you cannot have moved content you never dispatched.
  * `reason == NO_SCROLL_EFFECT ⇒ moved is False` — no-effect is inherently
    a dispatched-but-unmoved outcome.
  * `dispatched and reason == NOT_DISPATCHED` is invalid — NOT_DISPATCHED
    always means "nothing was sent".
  * `success == dispatched and moved` — the only success predicate.

`Reason` member set (PR1): the six mechanism/outcome members from item 1.
A proposed seventh member `COMPOSITION_MISMATCH` (forced-strategy vs.
detection contradiction) is deliberately NOT added in PR1; it is gated on its
own test in a later PR and nothing here depends on it.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional


class Reason(Enum):
    """Outcome of a composite scroll action.

    Mechanism members name *how* movement was achieved (on the success
    path). The terminal-outcome members (`NO_SCROLL_EFFECT`,
    `NOT_DISPATCHED`) name *why* the action did not succeed. `SUCCESS` is
    deliberately NOT a member — see module docstring.
    """

    NEXT_PAGE = "next_page"                      # semantic next/load-more click moved content
    WHEEL_MOVED = "wheel_moved"                  # pointer.scroll (A029) moved content
    SCROLLBAR_DRAGGED = "scrollbar_dragged"      # pointer.drag (A028) moved content
    FOCUS_THEN_SCROLL = "focus_then_scroll"      # activate + wheel moved content
    NO_SCROLL_EFFECT = "no_scroll_effect"        # dispatched but nothing verified as moved
    NOT_DISPATCHED = "not_dispatched"            # primitive/control action was not sent


class Strategy(Enum):
    """Composable scroll strategy (item 2). Each maps onto a catalog
    primitive or a composite sequence consumed by `ScrollPolicy`.

    The raw primitives come from `target/action_catalog/actions_v1.py`:
    wheel = `pointer.scroll` (A029), scrollbar drag = `pointer.drag` (A028).
    `FOCUS_THEN_SCROLL` and `PAGINATION` are composites — they reuse the
    primitives/semantic actions and are not catalog entries themselves.
    """

    WHEEL = "wheel"                     # primitive: pointer.scroll (A029)
    SCROLLBAR_DRAG = "scrollbar_drag"   # primitive: pointer.drag (A028)
    FOCUS_THEN_SCROLL = "focus_then_scroll"  # composite: activate + A029
    PAGINATION = "pagination"           # semantic: click next/load-more control

    # Map a strategy to the mechanism-named Reason used on its success path.
    # This is the single attribution table; PR2's state machine matches on it.
    def success_reason(self) -> Reason:
        return {
            Strategy.WHEEL: Reason.WHEEL_MOVED,
            Strategy.SCROLLBAR_DRAG: Reason.SCROLLBAR_DRAGGED,
            Strategy.FOCUS_THEN_SCROLL: Reason.FOCUS_THEN_SCROLL,
            Strategy.PAGINATION: Reason.NEXT_PAGE,
        }[self]


@dataclass(frozen=True)
class ScrollResult:
    """Frozen envelope separating *dispatched* from *moved*.

    Fields:
        dispatched: bool   — a pointer primitive or control action was sent
                             (derived from the `ActionReceipt.ok` returned by
                             `target/action_dispatcher/dispatch.py`).
        moved: bool        — the Observer verified real content change
                             (produced ONLY by the Observer diff engine, never
                             from a coordinate delta or a raw `ok`).
        reason: Reason     — the outcome; mechanism member on success,
                             terminal member on failure.
        strategy_used: Strategy | None — the strategy whose dispatch was the
                             final attributed one (set on the success path;
                             None otherwise). Keeps `reason` on the outcome
                             axis (design preference, item 1 open question).
    """

    dispatched: bool
    moved: bool
    reason: Reason
    strategy_used: Optional[Strategy] = None

    def __post_init__(self) -> None:
        # moved cannot be true if nothing was dispatched.
        if self.moved and not self.dispatched:
            raise ValueError("moved=True requires dispatched=True")
        # no_scroll_effect is inherently a "dispatched but unmoved" outcome.
        if self.reason is Reason.NO_SCROLL_EFFECT and self.moved:
            raise ValueError("reason=NO_SCROLL_EFFECT requires moved=False")
        # not_dispatched always means nothing was sent.
        if self.reason is Reason.NOT_DISPATCHED and self.dispatched:
            raise ValueError("reason=NOT_DISPATCHED requires dispatched=False")
        # A success-path outcome requires moved to be True.
        if self.reason in _MECHANISM_REASONS and not self.moved:
            raise ValueError(
                f"reason={self.reason.value!r} is a movement outcome and "
                f"requires moved=True"
            )
        # strategy_used is only meaningful once something actually moved.
        if self.strategy_used is not None and not self.moved:
            raise ValueError("strategy_used requires moved=True")

    @property
    def success(self) -> bool:
        """The only success predicate. Moved content is the goal; a
        dispatched-but-unmoved scroll is NOT a success."""
        return self.dispatched and self.moved

    @property
    def outcome(self) -> str:
        """Log/test convenience only; `reason` remains the authoritative
        fine-grained signal."""
        if self.success:
            return "moved"
        return "dispatched_only" if self.dispatched else "not_dispatched"

    # ---- serialization ---------------------------------------------------

    def to_dict(self) -> dict:
        from .pointer_result import reason_to_code

        # P0.3: `event_dispatched` aliases `dispatched` so the composite
        # envelope speaks the same vocabulary as the raw pointer envelope,
        # and `code` exposes a stable machine-readable code.
        return {
            "dispatched": self.dispatched,
            "event_dispatched": self.dispatched,
            "moved": self.moved,
            "reason": self.reason.value,
            "code": reason_to_code(self.reason.value),
            "success": self.success,
            "strategy_used": self.strategy_used.value if self.strategy_used else None,
        }

    @classmethod
    def success_result(cls, strategy: Strategy, *, reason: Optional[Reason] = None) -> "ScrollResult":
        """Convenience constructor for the success path."""
        return cls(
            dispatched=True,
            moved=True,
            reason=reason or strategy.success_reason(),
            strategy_used=strategy,
        )

    @classmethod
    def no_effect(cls) -> "ScrollResult":
        """A dispatched-but-unmoved terminal outcome."""
        return cls(
            dispatched=True, moved=False, reason=Reason.NO_SCROLL_EFFECT
        )

    @classmethod
    def not_dispatched(cls) -> "ScrollResult":
        """Nothing was ever armed."""
        return cls(
            dispatched=False, moved=False, reason=Reason.NOT_DISPATCHED
        )


# Mechanism-shaped reasons that require moved=True (used by __post_init__).
_MECHANISM_REASONS = frozenset({
    Reason.NEXT_PAGE,
    Reason.WHEEL_MOVED,
    Reason.SCROLLBAR_DRAGGED,
    Reason.FOCUS_THEN_SCROLL,
})


__all__ = [
    "Reason",
    "Strategy",
    "ScrollResult",
]
