"""
invalidation.py — Bridge: mutating actions invalidate observation
snapshots (architecture §8.4, FR-P1.1 lifecycle).

P0.3 exposes an in-process invalidation registry
(`observations.invalidate_snapshot(snapshot_id)`). P0.3 deliberately
did NOT wire it into mutating actions — that wiring is owned by
P1.1 dispatcher (this module).

When the dispatcher finishes a successful mutation action, it calls
`invalidate_after_mutation(backend_kind)` which marks every known
live snapshot as invalid. P1.1 simplification: there is one
"current" snapshot per backend; production deployment (multi-
session) will require per-snapshot bookkeeping, deferred to a later
checkpoint.

Stability (architecture §7.2):
  * `invalidate_after_mutation` is the only public entry point;
    adding more is a MINOR bump.
"""

from __future__ import annotations

from observations import invalidate_snapshot

# In P1.1 we treat every snapshot registered on the current backend
# as one bucket. The active_snapshot_id is set by `server.py`
# whenever `dump_tree` / `list_windows` produces a snapshot;
# `invalidate_after_mutation` clears it.
_active_snapshot_id: str | None = None


def set_active_snapshot(snapshot_id: str | None) -> None:
    """Record the snapshot_id most recently produced by the active
    backend. The dispatcher invalidates this snapshot after a
    mutating action completes successfully."""
    global _active_snapshot_id
    _active_snapshot_id = snapshot_id


def get_active_snapshot() -> str | None:
    return _active_snapshot_id


def invalidate_after_mutation(backend_kind: str) -> str | None:
    """Invalidate the active snapshot, if any.

    P1.1 simplification: there is one "current" snapshot per
    backend process (P1.x production deployment with multiple
    concurrent sessions is out of scope for the MVP). The
    dispatcher calls this after every successful mutating action
    so that any subsequent resolver call against the active
    snapshot is rejected with `target_stale`.

    Returns the invalidated snapshot_id for tracing. Tests use
    the return value to assert that invalidation actually
    happened.

    P1.1 review #1 (issue 4): the previous docstring implied the
    bridge invalidated every known live snapshot. The actual
    behaviour — and the behaviour this MVP commits to — is
    "invalidate the single active snapshot tracked by this
    module".
    """
    sid = _active_snapshot_id
    if sid is not None:
        invalidate_snapshot(sid)
    return sid


def reset_for_tests() -> None:
    """Test-only: drop the active-snapshot tracker."""
    global _active_snapshot_id
    _active_snapshot_id = None


__all__ = [
    "set_active_snapshot",
    "get_active_snapshot",
    "invalidate_after_mutation",
    "reset_for_tests",
]