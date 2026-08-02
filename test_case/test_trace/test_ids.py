"""P1.3 acceptance gate — event ids (FR-P1.3-10)."""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest


_REPO = Path(__file__).resolve().parents[2]
for p in (str(_REPO / "target"), str(_REPO / "agent")):
    if p not in sys.path:
        sys.path.insert(0, p)


from trace import (  # noqa: E402
    generate_branch_id,
    generate_call_id,
    generate_event_id,
    generate_plan_id,
    generate_trace_id,
)


def test_event_id_format():
    from trace.ids import reset_for_tests
    reset_for_tests()

    eid = generate_event_id(now_ns=1_700_000_000_000_000_000)
    assert eid.startswith("EVT-")
    # 20 hex after the prefix = 12 hex ms + 4 hex counter + 4 hex random.
    assert len(eid) == len("EVT-") + 20
    suffix = eid.split("-", 1)[1]
    assert re.fullmatch(r"[0-9a-f]{20}", suffix), suffix


def test_event_id_sortable_by_timestamp():
    """Two ids generated with strictly increasing `now_ns` must
    lex-sort in the same order (FR-P1.3-10: sortable unique IDs).

    Note: with random suffix equality, two ids generated in the
    same millisecond may collide on sort — the contract covers
    cross-session and across-ms ordering, not sub-millisecond
    ordering (UUIDv7 specifies a random tail for uniqueness).
    """
    from trace.ids import reset_for_tests
    reset_for_tests()

    e1 = generate_event_id(now_ns=1_000_000_000)
    e2 = generate_event_id(now_ns=2_000_000_000)
    e3 = generate_event_id(now_ns=100_000_000_000)
    assert e1 < e2 < e3


def test_event_id_uniqueness():
    """Same `now_ns`, different randoms — all distinct."""
    from trace.ids import reset_for_tests
    reset_for_tests()

    ids = {generate_event_id(now_ns=12345) for _ in range(50)}
    assert len(ids) == 50


def test_trace_id_format():
    tid = generate_trace_id()
    assert tid.startswith("TR-")
    assert len(tid) == len("TR-") + 20


def test_branch_id_format():
    bid = generate_branch_id()
    assert bid.startswith("BR-")
    assert len(bid) == len("BR-") + 20


def test_plan_id_format():
    pid = generate_plan_id()
    assert pid.startswith("PLAN-")
    assert len(pid) == len("PLAN-") + 20


def test_call_id_format():
    cid = generate_call_id()
    assert cid.startswith("CALL-")
    assert len(cid) == len("CALL-") + 20


def test_event_id_monotonic_same_ms():
    """P1.3 review #1 Blocker 1: same-ms events must still lex-sort
    in insertion order, because the counter increments while the
    random suffix alone would invert under ties."""
    from trace.ids import reset_for_tests
    reset_for_tests()
    ids = [
        generate_event_id(now_ns=1_700_000_000_000_000_000)
        for _ in range(100)
    ]
    # Lex-sorted == insertion order (same ms).
    assert ids == sorted(ids)
    # All unique.
    assert len(set(ids)) == 100
    # First 8 hex chars are equal (same ms); chars 8..12 increment.
    prefixes = [i[4:16] for i in ids]
    assert len(set(prefixes)) == 1, "ms portion must be identical"
    counters = [i[16:20] for i in ids]
    assert counters == [format(i, "04x") for i in range(100)]


def test_event_id_monotonic_across_ms():
    """Across strictly-increasing timestamps, the ms portion
    dominates lex-sort; the counter is invisible."""
    from trace.ids import reset_for_tests
    reset_for_tests()
    ids = [
        generate_event_id(now_ns=1_700_000_000_000_000_000 + i * 1_000_000)
        for i in range(10)
    ]
    assert ids == sorted(ids)


def test_event_id_unique_across_processes():
    """Two simultaneous ids on the same ms with the same counter
    are disambiguated by the random suffix."""
    from trace.ids import reset_for_tests
    reset_for_tests()
    ids = {generate_event_id(now_ns=1_000_000_000) for _ in range(50)}
    assert len(ids) == 50


def test_event_id_reset_for_tests():
    """`reset_for_tests` lets test fixtures start fresh."""
    from trace.ids import reset_for_tests
    reset_for_tests()
    a = generate_event_id(now_ns=1_000_000)
    reset_for_tests()
    b = generate_event_id(now_ns=1_000_000)
    # Different random suffix in the new run, but the same
    # format and lex-sortable.  We don't require equality because
    # the random suffix is genuinely random.
    assert a != b
    assert len(a) == len(b)


def test_event_id_64k_counter_overflow_advances_ms():
    """When the per-ms counter exceeds 0xFFFF the timestamp must
    advance to keep monotonicity."""
    from trace.ids import reset_for_tests
    reset_for_tests()
    # Generate 65,539 ids in the same ms; the last few should
    # land in the next ms.
    base_ms = 2_000_000_000  # = 0x77359400 in 8 hex
    ids = [generate_event_id(now_ns=base_ms * 1_000_000) for _ in range(0xFFFF + 4)]
    # All distinct.
    assert len(set(ids)) == 0xFFFF + 4
    # The last id's ms portion must be > base_ms (counter overflow
    # bumped us into the next millisecond).
    assert int(ids[-1][4:16], 16) > base_ms