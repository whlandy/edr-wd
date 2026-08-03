"""trace_adapter.py — forwards RequestedEvent → TraceStore.append (P2.2 — Commit E.2).

The :class:`RecoveryExecutor` (Commit D) emits a
``tuple[RequestedEvent, ...]`` as part of its
:class:`ExecutionOutcome`. Commit E.2 forwards those events
to a :class:`TraceStore` so the recovery cycle appears in the
trace chain.

The adapter is intentionally thin:

    * It maps ``event_type`` strings to :class:`EventType`
      enum values.
    * It populates the trace event envelope (``step_id``)
      from the payload.
    * It does **not** construct or modify :class:`RequestedEvent`
      payloads.

Branch binding:

    A :class:`TraceStore` is bound to ONE branch (opened via
    :meth:`TraceStore.open`). The adapter holds a single
    store instance; callers are responsible for opening a
    store on the recovery branch before instantiating the
    adapter.

    To bind a different branch, construct a new adapter with
    the new store.

Design doc: ``docs/requirements/P2-recovery-planner.md`` §4.3
(trace event producer boundary).
"""

from __future__ import annotations

from typing import Iterable

from agent.execution.recovery_events import (
    EVENT_TYPE_RECOVERY_REQUESTED,
    EVENT_TYPE_RECOVERY_RESULT,
    RequestedEvent,
)
from agent.trace.events import EventType
from agent.trace.store import TraceStore


# ---------------------------------------------------------------------------
# Dispatch table
# ---------------------------------------------------------------------------


_EVENT_TYPE_MAP: dict[str, EventType] = {
    EVENT_TYPE_RECOVERY_REQUESTED: EventType.RECOVERY_REQUESTED,
    EVENT_TYPE_RECOVERY_RESULT: EventType.RECOVERY_RESULT,
}


# ---------------------------------------------------------------------------
# TraceStoreAdapter
# ---------------------------------------------------------------------------


class TraceStoreAdapter:
    """Forwards :class:`RequestedEvent` instances to a
    :class:`TraceStore`.

    Use::

        adapter = TraceStoreAdapter(store)
        adapter.forward(outcome.events)

    The adapter calls :meth:`TraceStore.append_dict` once per
    event. The trace store handles chain hashing, sequence
    numbering, and persisted output.

    The adapter is **stateless beyond the store reference**.
    It is safe to construct one per ``RecoveryExecutor``
    instance.
    """

    def __init__(self, store: TraceStore) -> None:
        self._store = store

    def forward(
        self,
        events: Iterable[RequestedEvent],
    ) -> list[EventType]:
        """Append every event in ``events`` to the store.

        Returns the list of :class:`EventType` values that were
        forwarded (useful for tests / logging). Unknown event
        types are skipped and logged via a :class:`ValueError`
        in strict mode, or returned as ``None`` in non-strict
        mode (see :meth:`forward_strict`).
        """
        forwarded: list[EventType] = []
        for event in events:
            et = self._dispatch(event)
            if et is not None:
                forwarded.append(et)
        return forwarded

    def forward_strict(
        self,
        events: Iterable[RequestedEvent],
    ) -> list[EventType]:
        """Like :meth:`forward` but raises on unknown event types."""
        forwarded: list[EventType] = []
        for event in events:
            et = self._dispatch_strict(event)
            forwarded.append(et)
        return forwarded

    # -----------------------------------------------------------------
    # Dispatch
    # -----------------------------------------------------------------

    def _dispatch(self, event: RequestedEvent) -> EventType | None:
        """Forward one event; return ``None`` for unknown types."""
        et = _EVENT_TYPE_MAP.get(event.event_type)
        if et is None:
            return None
        self._write(et, event)
        return et

    def _dispatch_strict(self, event: RequestedEvent) -> EventType:
        """Forward one event; raise on unknown types."""
        et = _EVENT_TYPE_MAP.get(event.event_type)
        if et is None:
            raise ValueError(
                f"unknown recovery event type: {event.event_type!r}"
            )
        self._write(et, event)
        return et

    def _write(self, et: EventType, event: RequestedEvent) -> None:
        """Write one event to the trace store."""
        payload = event.to_dict()
        # ``step_id`` lives both in the payload and on the trace
        # envelope (TraceStore requires it on the envelope).
        step_id = payload.get("step_id")
        self._store.append_dict(
            et,
            payload=payload,
            step_id=step_id if isinstance(step_id, str) else None,
        )


__all__ = ["TraceStoreAdapter"]