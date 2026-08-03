"""trace_payload.py — TracePayload Protocol (P2.2 — Commit G, N3 refactor).

Round 2 review (post Commit F) suggested replacing the growing
:class:`RequestedEvent.payload` union with a :class:`TracePayload`
Protocol once projections start consuming events. Commit G is
that consumer.

Design:

    class TracePayload(Protocol):
        def to_dict(self) -> dict[str, object]: ...

All four typed payloads in P2.2 implement this protocol:

    * :class:`RecoveryRequestedPayload`
    * :class:`RecoveryResultPayload`
    * :class:`BranchCreatedPayload`
    * :class:`ReplanCreatedPayload`

Commit G adds new payloads (e.g.
:class:`CheckpointCreatedPayload`,
:class:`ProjectionUpdatedPayload`) which all conform to the
same protocol.

This module defines the protocol only — no runtime classes
are added. Downstream code can use ``isinstance`` only if
explicit ``@runtime_checkable`` is desired; the protocol is
designed for static type checking (the runtime contract is
the ``to_dict()`` method).
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class TracePayload(Protocol):
    """Wire-serializable payload for a trace event.

    Implementations must:

    * Be immutable (``frozen=True`` dataclass recommended).
    * Expose a ``to_dict() -> dict[str, object]`` method.
    * Be deterministic: ``to_dict()`` must produce the same
      output for the same input (no timestamps, no random ids).
    * Round-trip through JSON: ``json.loads(json.dumps(p.to_dict()))``
      must yield the same fields with the same types.
    """

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-serializable mapping of the payload."""
        ...


__all__ = ["TracePayload"]