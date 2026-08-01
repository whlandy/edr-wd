"""
cache.py — Bounded LRU idempotency cache + per-request inflight
lock (P1.1, FR-P1.1-02, -08 + P1.1 review #1 race-condition fix).

P1.1 dispatcher's idempotency contract:

    * `lookup(request_id)` returns the cached receipt, or None.
    * `store(request_id, receipt)` inserts (or overwrites) the entry.
    * `inflight_lock(request_id)` is a context manager that
      serialises concurrent `dispatch()` calls sharing the same
      `request_id`. The first caller acquires the lock and runs
      the backend; the second caller blocks until the first
      finishes, then sees the cached receipt via the regular
      `lookup`. The lock is released even if the backend call
      raises.

Eviction is strict LRU; `max_entries` defaults to 1024.

The lock map grows monotonically with the number of distinct
request_ids seen concurrently. After a request finishes its lock
is removed from the map (so the map only holds the active inflight
set, never the historical one). This avoids an unbounded leak.

Stability (architecture §7.2):
  * `max_entries` is configurable per process; changing the
    default is a MINOR bump.
  * Adding a new method to the cache surface is a MINOR bump.
  * Adding/removing inflight lock semantics is a MINOR bump
    (the public API is `inflight_lock(request_id)`; its internal
    implementation may evolve).
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from contextlib import contextmanager
from typing import Any, Iterator


_lock = threading.Lock()
_cache: "OrderedDict[str, Any]" = OrderedDict()
_inflight: dict[str, threading.Lock] = {}
DEFAULT_MAX_ENTRIES = 1024


# ---------------------------------------------------------------------------
# Idempotency cache (FR-P1.1-02, -08)
# ---------------------------------------------------------------------------


def lookup(request_id: str) -> Any | None:
    """Return the cached receipt for `request_id`, or None if
    absent. A successful lookup marks the entry as recently used
    so the LRU eviction policy keeps it around longer."""
    if not isinstance(request_id, str) or not request_id:
        return None
    with _lock:
        receipt = _cache.get(request_id)
        if receipt is not None:
            _cache.move_to_end(request_id)
        return receipt


def store(request_id: str, receipt: Any, *,
          max_entries: int = DEFAULT_MAX_ENTRIES) -> None:
    """Insert (or overwrite) the cache entry.

    When `len(_cache) >= max_entries`, the oldest entry is evicted
    first. With the default cap of 1024, the cache holds at most
    1024 receipts.
    """
    if not isinstance(request_id, str) or not request_id:
        return
    with _lock:
        if request_id in _cache:
            _cache.move_to_end(request_id)
            _cache[request_id] = receipt
            return
        _cache[request_id] = receipt
        if len(_cache) > max_entries:
            _cache.popitem(last=False)  # FIFO eviction at front


def lru_size() -> int:
    """Current entry count (for tests and metrics)."""
    with _lock:
        return len(_cache)


def lru_capacity_remaining(max_entries: int = DEFAULT_MAX_ENTRIES) -> int:
    """Number of additional entries the cache can hold before the
    next eviction. Test helper."""
    with _lock:
        return max(0, max_entries - len(_cache))


# ---------------------------------------------------------------------------
# Per-request inflight lock (P1.1 review #1)
# ---------------------------------------------------------------------------


@contextmanager
def inflight_lock(request_id: str) -> Iterator[None]:
    """Serialise concurrent dispatch() calls sharing the same
    `request_id`. The first caller acquires the lock; concurrent
    callers block until the first caller's `with` block exits.

    Use this BEFORE calling `lookup` / `store` so that the canonical
    flow is:

        with inflight_lock(request_id):
            cached = lookup(request_id)
            if cached is not None:
                return cached
            receipt = _do_backend_work(...)
            store(request_id, receipt)
            return receipt

    The lock map is pruned on entry/exit so it holds only the
    active inflight set; no historical locks accumulate.
    """
    if not isinstance(request_id, str) or not request_id:
        # No serialisation possible for malformed IDs; yield an
        # uncontended scope so the caller still runs. The caller
        # is expected to surface `invalid_request_id` separately.
        yield
        return
    with _lock:
        lock = _inflight.get(request_id)
        if lock is None:
            lock = threading.Lock()
            _inflight[request_id] = lock
        release_lock = False
        # We need to know whether we created the lock so we can
        # prune it on exit; acquire the lock first.
    lock.acquire()
    try:
        yield
    finally:
        lock.release()
        # Prune: only the last releaser removes the entry.
        with _lock:
            # If nobody else is waiting (no contention), the
            # entry can be safely removed.
            if not lock.locked() and _inflight.get(request_id) is lock:
                _inflight.pop(request_id, None)


def inflight_count() -> int:
    """Number of request_ids currently being processed. Test helper."""
    with _lock:
        return len(_inflight)


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


def clear() -> None:
    """Drop the entire cache AND the inflight map. Used by tests."""
    with _lock:
        _cache.clear()
        _inflight.clear()


__all__ = [
    "DEFAULT_MAX_ENTRIES",
    "lookup",
    "store",
    "clear",
    "lru_size",
    "lru_capacity_remaining",
    "inflight_lock",
    "inflight_count",
]