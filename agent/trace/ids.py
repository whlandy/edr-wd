"""
ids.py — Sortable unique event identifiers (FR-P1.3-10).

P1.3 mandates UUIDv7 or ULID semantics. UUIDv7/ULID's essential
property is *monotonicity within a timestamp window*: ids
generated in the same millisecond must still lex-sort in the
order they were emitted. We achieve that with a process-local
monotonic counter:

    EVT-<12 hex ms> <4 hex counter> <4 hex random>

Format (20 hex chars after the prefix):

  * 12 hex ms      — milliseconds since Unix epoch.
  * 4 hex counter  — per-process counter that increments
                     within the same millisecond; if the counter
                     overflows (0xffff) we bump to the next ms.
  * 4 hex random   — fresh randomness so ids across processes
                     do not collide on the same (ms, counter).

Within one process the order of generation matches lex order.
Across processes sharing a wall clock, ids still sort by ms
first (with random tiebreak).

P1.3 review #1 Blocker 1: the previous implementation used only
ms + random, which lost same-ms ordering whenever two random
suffixes inverted. The monotonic counter fixes that.
"""

from __future__ import annotations

import os
import threading
import time
import uuid

PREFIX = "EVT"
COUNTER_MAX = 0xFFFF  # 4 hex digits
_LAST_STATE: dict[str, tuple[int, int]] = {}
_LAST_LOCK = threading.Lock()


def _next_pair(now_ns: int) -> tuple[int, int, str]:
    """Return (ms, counter, random_hex4) for one id.

    Thread-safe: a single module-level lock serialises the
    (last_ms, counter) state across the process.
    """
    ms = now_ns // 1_000_000
    with _LAST_LOCK:
        last = _LAST_STATE.get(PREFIX)
        if last is None or ms > last[0]:
            counter = 0
        elif ms == last[0]:
            counter = last[1] + 1
            if counter > COUNTER_MAX:
                # Counter exhausted: deterministically bump to
                # the next millisecond so the new id stays greater
                # than any previously-emitted id in this process.
                ms += 1
                counter = 0
        else:
            # Clock went backwards (NTP, suspend/resume) — or the
            # previous call bumped ms because the counter had
            # overflowed.  Either way, the last-emitted timestamp
            # is the floor: we advance `ms` past `last[0]` so the
            # new id is strictly greater than every prior id in
            # this process.
            ms = last[0] + 1
            counter = last[1] + 1
            if counter > COUNTER_MAX:
                ms += 1
                counter = 0
        _LAST_STATE[PREFIX] = (ms, counter)
    return ms, counter, uuid.uuid4().hex[:4]


def _format(prefix: str, ms: int, counter: int, random_hex: str) -> str:
    # Layout (20 hex after the prefix):
    #   12 hex ms       (covers up to ~year 500,000)
    #   4 hex counter   (per-process monotonic within a millisecond)
    #   4 hex random    (cross-process uniqueness)
    return f"{prefix}-{ms:012x}{counter:04x}{random_hex}"


def generate_event_id(now_ns: int | None = None) -> str:
    """Return a sortable unique id like ``EVT-66b3a4f10001abcd``.

    Monotonic in a single process: same-ms emissions sort by
    insertion order (counter increments). Cross-process ids
    still sort by ms first.
    """
    if now_ns is None:
        now_ns = time.time_ns()
    ms, counter, random_hex = _next_pair(now_ns)
    return _format(PREFIX, ms, counter, random_hex)


def _other(prefix: str, now_ns: int | None = None) -> str:
    if now_ns is None:
        now_ns = time.time_ns()
    ms, counter, random_hex = _next_pair(now_ns)
    return _format(prefix, ms, counter, random_hex)


def generate_trace_id(now_ns: int | None = None) -> str:
    """Return ``TR-<16 hex>`` for trace identifiers."""
    return _other("TR", now_ns)


def generate_branch_id(now_ns: int | None = None) -> str:
    """Return ``BR-<16 hex>`` for branch identifiers."""
    return _other("BR", now_ns)


def generate_plan_id(now_ns: int | None = None) -> str:
    """Return ``PLAN-<16 hex>`` for plan identifiers."""
    return _other("PLAN", now_ns)


def generate_call_id(now_ns: int | None = None) -> str:
    """Return ``CALL-<16 hex>`` for call identifiers."""
    return _other("CALL", now_ns)


def reset_for_tests() -> None:
    """Reset the monotonic state. Tests call this in setup."""
    with _LAST_LOCK:
        _LAST_STATE.clear()


__all__ = [
    "PREFIX",
    "generate_event_id",
    "generate_trace_id",
    "generate_branch_id",
    "generate_plan_id",
    "generate_call_id",
    "reset_for_tests",
]