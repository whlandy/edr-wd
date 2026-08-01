"""
invalidate.py — Snapshot invalidation registry (architecture §8.4,
FR-P0.3-08).

The registry tracks which snapshot IDs are still considered live.
P0.3 exposes the API; wiring it to mutating actions (P1.1 dispatcher)
is deferred per the requirements doc:

    "Calling invalidate_snapshot() makes references to that snapshot
     resolve as target_stale; P0.3 does not modify legacy action
     wrappers to invoke it."

Operations:

    * `mark_live(snapshot_id)` — record a snapshot as currently valid.
    * `mark_invalid(snapshot_id)` — record a snapshot as invalid
      (subsequent resolve_target calls against this snapshot_id
      return `target_stale`).
    * `is_live(snapshot_id) -> bool` — fast lookup.
    * `invalidate_snapshot(snapshot_id)` — convenience wrapper
      equivalent to `mark_invalid`.

Thread safety: a module-level `_lock` guards the dict. P0.3 only
needs thread-safe bookkeeping; the snapshot itself is immutable.
"""

from __future__ import annotations

import threading


_lock = threading.Lock()
_live: dict[str, bool] = {}


def mark_live(snapshot_id: str) -> None:
    """Record `snapshot_id` as currently valid."""
    with _lock:
        _live[snapshot_id] = True


def mark_invalid(snapshot_id: str) -> None:
    """Record `snapshot_id` as invalid. The dict retains the entry
    so a later `is_live` call returns False instead of KeyError —
    the resolver uses `is_live` to detect stale references."""
    with _lock:
        _live[snapshot_id] = False


def invalidate_snapshot(snapshot_id: str) -> None:
    """Convenience wrapper for `mark_invalid`."""
    mark_invalid(snapshot_id)


def is_live(snapshot_id: str) -> bool:
    """True iff `snapshot_id` has been marked live and not yet
    invalidated. A snapshot_id that has never been seen returns
    False (treating it as stale)."""
    with _lock:
        return _live.get(snapshot_id, False) is True


def forget(snapshot_id: str) -> None:
    """Drop the snapshot entry entirely (memory cleanup). After
    `forget`, `is_live` returns False."""
    with _lock:
        _live.pop(snapshot_id, None)


def reset_for_tests() -> None:
    """Clear the entire registry. Test-only helper — not part of the
    public API; intentionally not exported via __all__."""
    with _lock:
        _live.clear()


__all__ = [
    "mark_live",
    "mark_invalid",
    "invalidate_snapshot",
    "is_live",
    "forget",
]