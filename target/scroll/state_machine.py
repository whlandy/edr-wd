"""
state_machine.py — the Step enum, NavigateContext, transition, and the driver.
(docs/todo/scroll-and-paged-table-actions.md, enforceability item 4; PR2.)

The whole composite lifecycle is a single small deterministic state machine,
not a free-form chain of `if/elif`. Terminal outcomes are exactly
`SUCCESS | NO_SCROLL_EFFECT | NOT_DISPATCHED`; `FALLBACK` is the only
non-terminal loop-back edge (`FALLBACK -> EXECUTE`).

Division of labour (item 4 "transition() only DECIDES"):

  * `transition(s, ctx) -> Step` is PURE — it reads the frozen `ctx` and
    returns the next state. It never dispatches, never verifies, holds no UI
    state, so a table-driven test enumerates every `(state, ctx-class)` edge
    and asserts the successor (item-4 determinism invariant).
  * `run(ctx, dispatch, verify) -> ScrollResult` is the driver. It performs
    the side-effecting EXECUTE (one `scroll_with_policy` call per position,
    capped at `policy.max_attempts`), updates the immutable `ctx`, and calls
    `transition` to decide the next step. It owns no strategy-iteration
    decision independent of `transition`.

PR2 stub boundary: `CLASSIFY`/`OWNERSHIP` are the hard-coded `FLAT -> WHEEL`
mapping (no `PageDetector` yet). The driver therefore seeds `ctx.remaining`
with `policy.strategy_order` and EXECUTE always attempts `remaining[0]`.
`PAGINATION` / `FOCUS_THEN_SCROLL` merely "resolve to nothing armed" via the
injected `dispatch` until PR3 wires detection.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional, Protocol, Tuple

try:
    from ..action_dispatcher.receipts import ActionReceipt
    from ..observations.models import ObservationSnapshot
except ImportError:
    from action_dispatcher.receipts import ActionReceipt
    from observations.models import ObservationSnapshot

from .policy import ScrollPolicy, scroll_with_policy
from .results import Reason, ScrollResult, Strategy


class Step(Enum):
    """State in the composite lifecycle (item 4).

    `DISCOVER .. VERIFY` are non-terminal. `FALLBACK` is the single
    non-terminal loop-back edge. The last three are the only terminal states.
    """

    DISCOVER = "discover"
    CLASSIFY = "classify"
    OWNERSHIP = "ownership"
    EXECUTE = "execute"
    VERIFY = "verify"
    FALLBACK = "fallback"
    SUCCESS = "success"
    NO_SCROLL_EFFECT = "no_scroll_effect"
    NOT_DISPATCHED = "not_dispatched"


TERMINAL_STEPS = frozenset(
    {Step.SUCCESS, Step.NO_SCROLL_EFFECT, Step.NOT_DISPATCHED}
)


# Injected verify callback (polymorphic over dispatch): returns True if content
# actually moved since the last snapshot. Wraps PR1's `Observer`.
VerifyFn = object


@dataclass(frozen=True)
class NavigateContext:
    """Immutable machine context. The driver replaces it each step.

    The fifth field `detection` is deliberately **reserved as the planned PR3
    slot** for `DetectionResult` — it is present now (as `None`) so PR3's
    reordered `strategy_order` / `DetectionResult` slots in without signature
    churn (design item 4 / PR2 open question).

    Fields:
        owner: the A012-held owner id that owns the navigation.
        msg_id: optional caller message id (for attribution/logging).
        strategy_ref: opaque reference passed through to `dispatch` (e.g. a
            resolved `Target`); unused by the machine itself.
        policy: the `ScrollPolicy` carrying the bounds.
        remaining: strategy positions not yet exhausted; the current position
            is `remaining[0]`.
        position_attempts: how many times the *current* position has been
            attempted (bounded by `policy.max_attempts`).
        any_armed: True if any EXECUTE ever armed a receipt (drives the
            `NO_SCROLL_EFFECT` vs `NOT_DISPATCHED` terminal discipline).
        attempt: the `ScrollResult` of the last EXECUTE (None before the
            first EXECUTE).
        detection: RESERVED — `None` in PR2; PR3 slots `DetectionResult` here.
    """

    owner: str
    policy: ScrollPolicy
    remaining: Tuple[Strategy, ...]
    msg_id: Optional[str] = None
    strategy_ref: Optional[object] = None
    detection: Optional[object] = None
    position_attempts: int = 0
    any_armed: bool = False
    attempt: Optional[ScrollResult] = None

    @classmethod
    def start(
        cls,
        owner: str,
        policy: ScrollPolicy,
        *,
        msg_id: Optional[str] = None,
        strategy_ref: Optional[object] = None,
        detection: Optional[object] = None,
    ) -> "NavigateContext":
        """Seed a fresh context at DISCOVER with the full strategy_order."""
        return cls(
            owner=owner,
            policy=policy,
            remaining=policy.strategy_order,
            msg_id=msg_id,
            strategy_ref=strategy_ref,
            detection=detection,
        )

    def refresh(self) -> "NavigateContext":
        """Return a new context with the same owner/msg_id/policy but a fresh
        strategy_order and cleared execution state. Used at the start of a new
        top-level navigation (a new snapshot pair / a new page)."""
        return NavigateContext.start(
            self.owner,
            self.policy,
            msg_id=self.msg_id,
            strategy_ref=self.strategy_ref,
        )


def transition(state: Step, ctx: NavigateContext) -> Step:
    """Pure decision rule: map `(state, ctx)` to the next `Step`.

    PURE — reads `ctx` only, never dispatches/verifies. This is the single
    place strategy iteration is decided (one STRATEGY-ITERATION edge).
    """
    if state is Step.DISCOVER:
        return Step.CLASSIFY

    if state is Step.CLASSIFY:
        return Step.OWNERSHIP

    if state is Step.OWNERSHIP:
        # No eligible strategy position remains ⇒ nothing can ever be armed.
        return Step.EXECUTE if ctx.remaining else Step.NOT_DISPATCHED

    if state is Step.EXECUTE:
        # One bounded EXECUTE always verifies (VERIFY is never optional).
        return Step.VERIFY

    if state is Step.VERIFY:
        # moved ⇒ terminal SUCCESS; PageChange.UNCERTAIN maps to not-moved.
        if ctx.attempt is not None and ctx.attempt.moved:
            return Step.SUCCESS
        return Step.FALLBACK

    if state is Step.FALLBACK:
        # Decide retry-vs-advance-vs-terminate. A NOT_DISPATCHED attempt
        # (nothing armed) always advances — there is no point retrying a
        # strategy that could not be armed. A NO_SCROLL_EFFECT attempt retries
        # the same position until `policy.max_attempts`, then advances.
        not_dispatched_attempt = (
            ctx.attempt is not None and ctx.attempt.reason is Reason.NOT_DISPATCHED
        )
        if not not_dispatched_attempt and ctx.position_attempts < ctx.policy.max_attempts:
            # Retry the current position (falls through to EXECUTE).
            return Step.EXECUTE
        # Advance exactly one strategy position.
        advanced = ctx.remaining[1:]
        if not advanced:
            # Order exhausted: distinguish the two terminal classes.
            return (
                Step.NO_SCROLL_EFFECT if ctx.any_armed else Step.NOT_DISPATCHED
            )
        return Step.EXECUTE

    # A terminal state has no successor.
    if state in TERMINAL_STEPS:
        raise ValueError(f"terminal state {state.name!r} has no successor")
    raise ValueError(f"unknown state {state!r}")


def run(
    ctx: NavigateContext,
    dispatch,
    verify,
    *,
    step_fn=None,
) -> ScrollResult:
    """Driver: execute side effects + decide via `transition`.

    One DISCOVER->CLASSIFY->OWNERSHIP prelude, then the EXECUTE/VERIFY/FALLBACK
    loop, terminating in exactly one of the three terminal outcomes. Total
    dispatches are provably bounded by `len(strategy_order) * max_attempts`.

    The FALLBACK bookkeeping (retry vs advance) lives here so the driver can
    produce a fresh immutable `ctx` for each EXECUTE; the *decision rule* for
    that bookkeeping is exactly what `transition(Step.FALLBACK, ctx)` encodes,
    and is kept in lock-step here (the single STRATEGY-ITERATION edge).

    f6 / single-execution unification: the machine remains the DECISION engine
    and delegates every EXECUTE to ONE execution engine — by default
    `scroll_with_policy` (the classic PR2 body); when `step_fn` is supplied it
    replaces that body so the new-architecture `ScrollCoordinator.step` can be
    driven by the machine. `step_fn(ctx) -> ScrollResult` must return a
    `ScrollResult` whose `moved` / `reason` the machine's `transition` already
    understands (SUCCESS via moved=True; NOT_DISPATCHED via a not-dispatched
    attempt; otherwise NO_SCROLL_EFFECT). `NavigateContext.detection` is
    passed through so the coordinator reads the machine's cached detection.

    Args:
        ctx: a `NavigateContext` (use `NavigateContext.start(...)`).
        dispatch: callable `(strategy=, amount=, strategy_ref=) -> ActionReceipt`
            exactly matching `scroll_with_policy`'s dispatch contract.
        verify: callable `() -> bool`; True iff content actually moved.
        step_fn: optional `(ctx) -> ScrollResult` overriding the EXECUTE body
            (the f6 single-execution bridge to ScrollCoordinator.step).
    """
    state = Step.DISCOVER

    while state not in TERMINAL_STEPS:
        if state is Step.EXECUTE:
            if step_fn is not None:
                res = step_fn(ctx)
            else:
                strategy = ctx.remaining[0]
                res = scroll_with_policy(
                    policy=ctx.policy,
                    verify=verify,
                    dispatch=dispatch,
                    strategy=strategy,
                    strategy_ref=ctx.strategy_ref,
                )
            armed = res.dispatched
            ctx = NavigateContext(
                owner=ctx.owner,
                policy=ctx.policy,
                remaining=ctx.remaining,
                msg_id=ctx.msg_id,
                strategy_ref=ctx.strategy_ref,
                detection=ctx.detection,
                position_attempts=ctx.position_attempts + 1,
                any_armed=ctx.any_armed or armed,
                attempt=res,
            )
            state = Step.VERIFY
            continue

        if state is Step.FALLBACK:
            # ---- single STRATEGY-ITERATION decision (lock-step w/ table) ----
            not_dispatched_attempt = (
                ctx.attempt is not None
                and ctx.attempt.reason is Reason.NOT_DISPATCHED
            )
            if not not_dispatched_attempt and ctx.position_attempts < ctx.policy.max_attempts:
                # Retry the current position (ctx unchanged except attempts).
                state = Step.EXECUTE
                continue
            # Advance exactly one strategy position.
            advanced = ctx.remaining[1:]
            if not advanced:
                state = (
                    Step.NO_SCROLL_EFFECT if ctx.any_armed else Step.NOT_DISPATCHED
                )
                break
            ctx = NavigateContext(
                owner=ctx.owner,
                policy=ctx.policy,
                remaining=advanced,
                msg_id=ctx.msg_id,
                strategy_ref=ctx.strategy_ref,
                detection=ctx.detection,
                position_attempts=0,
                any_armed=ctx.any_armed,
                attempt=None,
            )
            state = Step.EXECUTE
            continue

        # Pure prelude/decision edges.
        state = transition(state, ctx)

    # Terminal: return the terminal ScrollResult.
    if state is Step.SUCCESS:
        assert ctx.attempt is not None and ctx.attempt.moved
        return ctx.attempt
    if state is Step.NO_SCROLL_EFFECT:
        return ScrollResult.no_effect()
    # NOT_DISPATCHED
    return ScrollResult.not_dispatched()


def machine_step_fn(coordinator, snapshot, mode=None):
    """f6 single-execution bridge: adapt `ScrollCoordinator.step` to the
    machine's `step_fn(ctx)` contract.

    Returns a callable `(ctx) -> ScrollResult` for `run(..., step_fn=...)`.
    It hands the coordinator the machine's cached `DetectionResult`
    (from `NavigateContext.detection`) plus the given before-`snapshot`, so
    the machine DRIVES (DECIDE/advance-through-transition) and the
    `ScrollCoordinator` is the ONE execution engine (plan -> real dispatcher
    -> bounded verify). This is exactly the "machine drives; coordinator
    executes" unification f6 asks for.
    """
    from .verifier import ScrollCoordinator as _SC
    from .controller import NavigationMode

    if mode is None:
        mode = NavigationMode.MOVE_NEXT

    def _step(ctx: NavigateContext) -> ScrollResult:
        assert isinstance(coordinator, _SC)
        coordinator.set_detection(ctx.detection)  # type: ignore[arg-type]
        try:
            return coordinator.step(mode, snapshot, ctx.policy)
        finally:
            coordinator.clear_detection()

    return _step


__all__ = [
    "Step",
    "NavigateContext",
    "transition",
    "run",
    "TERMINAL_STEPS",
    "machine_step_fn",
]
