"""restore_dispatch.py — concrete RestoreHandler implementations (P2.2 — Commit E.1).

The :class:`RecoveryExecutor` (Commit D) dispatches via an
injected :class:`RestoreHandler`. Commit E.1 fills that DI
seam with one callable per :class:`RestoreStrategy`:

    | Strategy           | Handler              |
    | REDRIVE            | :class:`RedriveHandler`     |
    | RECONNECT          | :class:`ReconnectHandler`   |
    | PROCESS_RESTART    | :class:`ProcessRestartHandler` |
    | REOBSERVE_REPLAN   | :class:`ReobserveReplanHandler` |
    | BLOCKED            | (unreachable — planner) |

Each handler is a callable returning a :class:`RestoreResult`.
Handlers receive a ``payload`` mapping carrying ``failure``
(:class:`FailureContext`), ``plan`` (:class:`RecoveryPlan`),
and ``branch_id`` (the recovery branch id).

Design contract:

    * Handlers are **pure with respect to their inputs**: the
      ``failure`` / ``plan`` / ``branch_id`` are not mutated.
    * Handlers do **not** raise. They catch their own errors
      and surface them as :class:`RestoreResult(ok=False, code=...)`.
    * Handlers are **swappable** — :func:`make_restore_dispatch`
      is the single entry point. Callers inject the dependency
      graph (catalog executor, session re-lock) once and the
      factory builds the per-strategy dispatch table.

Boundary:

    * This module does **not** import :class:`TraceStore`.
      Trace forwarding is Commit E.2's job
      (:mod:`agent.execution.trace_adapter`).
    * This module does **not** import :class:`AtomicExecutor`.
      Catalog dispatch is delegated to an injected callable
      (see ``catalog_dispatch`` slot in
      :class:`RestoreDispatch`).
    * This module does **not** import :class:`InverseRegistry`.
      SOP lookup is the planner's job (Commit B). The
      :class:`ProcessRestartHandler` iterates the
      already-resolved ``plan.steps``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Mapping

from agent.execution.recovery import (
    FailureContext,
    RecoveryErrorCode,
    RestoreResult,
    RestoreStrategy,
)


# ---------------------------------------------------------------------------
# Dispatch callable types
# ---------------------------------------------------------------------------


CatalogDispatchFn = Callable[
    [str, Mapping[str, object]],
    RestoreResult,
]
"""Callable that runs one catalog action and returns its result.

The signature mirrors :class:`RestoreHandler` for catalog
actions — the dispatcher is responsible for mapping
``action_id`` to the actual backend call.

The :class:`RestoreDispatch` below does **not** call this
directly; the :class:`ProcessRestartHandler` is the only
consumer (it iterates ``SOPInverseAction.inverse_action_ids``
and dispatches each in order).
"""


SessionReconnectFn = Callable[
    [Mapping[str, object]],
    RestoreResult,
]
"""Callable that re-establishes a session lock.

Receives the recovery payload (so it can read
``failure.branch_id`` / context). Returns a
:class:`RestoreResult`.

If the implementation cannot re-lock the session (e.g. the
backend lost the lock state), return
``RestoreResult(ok=False, code=RecoveryErrorCode.RESTORE_LOCK_VERIFY_FAILED)``.
"""


# ---------------------------------------------------------------------------
# RestoreDispatch — the DI container
# ---------------------------------------------------------------------------


@dataclass
class RestoreDispatch:
    """Holds dependencies and produces per-strategy handlers.

    Attributes:
        catalog_dispatch: :class:`CatalogDispatchFn` —
            optional. If ``None``,
            :class:`ProcessRestartHandler` returns
            ``RESTORE_NOT_AVAILABLE`` because it cannot dispatch
            inverse actions. Tests inject a stub.
        session_reconnect: :class:`SessionReconnectFn` —
            optional. If ``None``,
            :class:`ReconnectHandler` returns
            ``RESTORE_LOCK_VERIFY_FAILED``.
    """

    catalog_dispatch: CatalogDispatchFn | None = None
    session_reconnect: SessionReconnectFn | None = None

    # Cached handler table, built lazily.
    _handlers: dict[RestoreStrategy, "RestoreHandler"] = field(
        default_factory=dict, init=False, repr=False,
    )

    def handler_for(self, strategy: RestoreStrategy) -> "RestoreHandler":
        """Return the :class:`RestoreHandler` for ``strategy``.

        The dispatch table is built once on first access; later
        calls return the cached callable. Mutating the
        dispatch dependencies after the first call will **not**
        affect the cached handlers — construct a fresh
        :class:`RestoreDispatch` if dependencies change.
        """
        if not self._handlers:
            self._build_handlers()
        try:
            return self._handlers[strategy]
        except KeyError:
            return self._blocked_fallback

    # -----------------------------------------------------------------
    # Build dispatch table
    # -----------------------------------------------------------------

    def _build_handlers(self) -> None:
        self._handlers = {
            RestoreStrategy.REDRIVE: RedriveHandler(),
            RestoreStrategy.RECONNECT: ReconnectHandler(
                session_reconnect=self.session_reconnect,
            ),
            RestoreStrategy.PROCESS_RESTART: ProcessRestartHandler(
                catalog_dispatch=self.catalog_dispatch,
            ),
            RestoreStrategy.REOBSERVE_REPLAN: ReobserveReplanHandler(),
        }

    @property
    def _blocked_fallback(self) -> "RestoreHandler":
        """Strategy missing from the dispatch table — treated
        as ``BLOCKED`` (this should never happen because
        ``RestoreStrategy.BLOCKED`` is unreachable from the
        planner, but we keep it as a safety net).
        """
        def _block(
            strategy: RestoreStrategy,
            payload: Mapping[str, object],
        ) -> RestoreResult:
            return RestoreResult(
                ok=False,
                code=RecoveryErrorCode.RESTORE_NOT_AVAILABLE,
                snapshot_id=None,
            )
        return _block


# ---------------------------------------------------------------------------
# RestoreHandler protocol alias (re-exported here for convenience)
# ---------------------------------------------------------------------------


# The protocol lives in :mod:`agent.execution.recovery_executor`.
# Import locally so the type is consistent.
RestoreHandler = Callable[
    [RestoreStrategy, Mapping[str, object]],
    RestoreResult,
]


# ---------------------------------------------------------------------------
# Concrete handlers
# ---------------------------------------------------------------------------


class RedriveHandler:
    """Re-runs the prior step sequence (REDRIVE strategy).

    REDRIVE means: the failure was caused by transient state
    drift; re-running the same step sequence is expected to
    recover. Commit E.1 returns a successful
    :class:`RestoreResult` with a synthetic ``snapshot_id``
    indicating "redrive".

    The actual re-execution is the caller's responsibility
    (typically the AtomicExecutor's main loop, which sees the
    ``REDRIVE`` plan and re-runs prior steps in order). This
    handler only **records** that the redrive was acknowledged.

    Production hardening (Commit G+) would inject the prior
    step list into ``payload`` and run them here. For Commit
    E.1 the executor's contract is met by always succeeding.
    """

    def __call__(
        self,
        strategy: RestoreStrategy,
        payload: Mapping[str, object],
    ) -> RestoreResult:
        if strategy is not RestoreStrategy.REDRIVE:
            return RestoreResult(
                ok=False,
                code=RecoveryErrorCode.RESTORE_NOT_AVAILABLE,
                snapshot_id=None,
            )
        return RestoreResult(
            ok=True,
            code=None,
            snapshot_id="snap-redrive",
        )


class ReconnectHandler:
    """Re-establishes a session lock (RECONNECT strategy).

    Delegates to an injected ``SessionReconnectFn``. The
    injected function is responsible for verifying the lock
    is held before returning success. The handler wraps the
    call in error handling so a handler exception never
    escapes the executor.
    """

    def __init__(self, session_reconnect: SessionReconnectFn | None) -> None:
        self._fn = session_reconnect

    def __call__(
        self,
        strategy: RestoreStrategy,
        payload: Mapping[str, object],
    ) -> RestoreResult:
        if strategy is not RestoreStrategy.RECONNECT:
            return RestoreResult(
                ok=False,
                code=RecoveryErrorCode.RESTORE_NOT_AVAILABLE,
                snapshot_id=None,
            )
        if self._fn is None:
            return RestoreResult(
                ok=False,
                code=RecoveryErrorCode.RESTORE_LOCK_VERIFY_FAILED,
                snapshot_id=None,
            )
        try:
            return self._fn(payload)
        except Exception:
            return RestoreResult(
                ok=False,
                code=RecoveryErrorCode.RESTORE_LOCK_VERIFY_FAILED,
                snapshot_id=None,
            )


class ProcessRestartHandler:
    """Runs SOP inverse actions in order (PROCESS_RESTART).

    Iterates ``plan.steps`` (already-resolved action ids
    produced by the planner) and calls ``catalog_dispatch``
    for each. The first failure short-circuits and surfaces
    the error code.

    The planner is the canonical owner of SOP inverse lookup;
    this handler is purely a runner.

    If ``plan.steps`` is empty, returns success (no inverse
    actions to run — a degenerate SOP case).
    If ``catalog_dispatch`` is None, returns
    :class:`RecoveryErrorCode.RESTORE_NOT_AVAILABLE`.
    """

    def __init__(
        self,
        catalog_dispatch: CatalogDispatchFn | None,
    ) -> None:
        self._dispatch = catalog_dispatch

    def __call__(
        self,
        strategy: RestoreStrategy,
        payload: Mapping[str, object],
    ) -> RestoreResult:
        if strategy is not RestoreStrategy.PROCESS_RESTART:
            return RestoreResult(
                ok=False,
                code=RecoveryErrorCode.RESTORE_NOT_AVAILABLE,
                snapshot_id=None,
            )

        if self._dispatch is None:
            return RestoreResult(
                ok=False,
                code=RecoveryErrorCode.RESTORE_NOT_AVAILABLE,
                snapshot_id=None,
            )

        # Extract the planner-resolved steps.
        plan = payload.get("plan")
        steps = getattr(plan, "steps", ())
        if not steps:
            # Degenerate case: planner emitted PROCESS_RESTART
            # with no inverse actions. Treat as success — the
            # caller will observe no work happened.
            return RestoreResult(
                ok=True,
                code=None,
                snapshot_id="snap-restart-empty",
            )

        for action_id in steps:
            try:
                step_result = self._dispatch(action_id, payload)
            except Exception:
                return RestoreResult(
                    ok=False,
                    code=RecoveryErrorCode.RESTORE_NOT_AVAILABLE,
                    snapshot_id=None,
                )
            if not step_result.ok:
                return step_result

        return RestoreResult(
            ok=True,
            code=None,
            snapshot_id="snap-restart",
        )


class ReobserveReplanHandler:
    """Marks the replan state AVAILABLE (REOBSERVE_REPLAN).

    Commit E.1 has no real reobservation logic — the executor
    itself already advances the replan state machine
    (``NOT_REQUESTED -> AVAILABLE -> CONSUMED``) in
    :class:`RecoveryExecutor.execute`. The handler just
    confirms the reobservation succeeded so the executor
    can complete the cycle.

    Returns success unconditionally; Commit E.2's TraceStore
    adapter captures the :class:`replan_created` event.
    """

    def __call__(
        self,
        strategy: RestoreStrategy,
        payload: Mapping[str, object],
    ) -> RestoreResult:
        if strategy is not RestoreStrategy.REOBSERVE_REPLAN:
            return RestoreResult(
                ok=False,
                code=RecoveryErrorCode.RESTORE_NOT_AVAILABLE,
                snapshot_id=None,
            )
        return RestoreResult(
            ok=True,
            code=None,
            snapshot_id="snap-reobserve",
        )


__all__ = [
    "RestoreDispatch",
    "RedriveHandler",
    "ReconnectHandler",
    "ProcessRestartHandler",
    "ReobserveReplanHandler",
    "CatalogDispatchFn",
    "SessionReconnectFn",
    "RestoreHandler",
]